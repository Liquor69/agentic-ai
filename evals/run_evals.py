#!/usr/bin/env python
"""
evals/run_evals.py

Evaluation harness for the Hybrid AI Operations Agent.

Usage:
    python evals/run_evals.py              # mock mode (default) — LLM bypassed
    python evals/run_evals.py --live       # live mode — uses real ANTHROPIC_API_KEY
    python evals/run_evals.py --filter faq # only run cases whose id contains 'faq'
    python evals/run_evals.py --verbose    # print per-case detail

Metrics:
    routing_accuracy      — % of cases where selected_tool ∈ expected_tools (live only)
    halt_reason_accuracy  — % of cases where halt_reason == expected_halt_reason
    policy_outcome_match  — % of cases where policy label matches expected_policy_outcome
    escalation_rate       — % of cases that halted with ambiguity or max_iterations
    deflection_rate       — % of cases resolved without escalation (success / pending / form)

Mock mode injects expected_tools directly into the planner, skipping the LLM routing step.
Live mode measures real LLM routing against expected_tools.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import yaml

# ── Path setup ────────────────────────────────────────────────────────────────
_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))


# ── DB isolation for evals ────────────────────────────────────────────────────

def _setup_eval_db() -> tempfile.TemporaryDirectory:
    """Create an isolated SQLite DB for the eval run. Returns the temp dir (keep alive)."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from db import crud, models
    from db.models import Base

    tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    db_url = f"sqlite:///{Path(tmp.name) / 'eval.db'}"
    engine = create_engine(db_url, connect_args={"check_same_thread": False})
    SL = sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False)
    Base.metadata.create_all(bind=engine)

    models.engine = engine
    models.SessionLocal = SL
    crud.SessionLocal = SL
    return tmp


# ── Tool registry setup ───────────────────────────────────────────────────────

def _setup_tools() -> None:
    from tools.registry import _REGISTRY
    _REGISTRY.clear()
    for key in list(sys.modules):
        if key.startswith("tools.") and key != "tools.registry":
            del sys.modules[key]
    import tools.customer_ops   # noqa: F401
    import tools.webhooks       # noqa: F401
    import tools.external_api   # noqa: F401


# ── Mock LLM helpers ──────────────────────────────────────────────────────────

def _mock_classify(text: str = "request"):
    return {"content": text, "tool_use": [], "stop_reason": "end_turn", "usage": {}}


def _mock_select(tools: list[dict]):
    return {
        "stop_reason": "tool_use",
        "tool_use": [{"id": "eval-mock", "name": "execute_plan", "input": {"tools": tools}}],
        "content": "",
        "usage": {},
    }


def _mock_faq(answer: str = "See FAQ."):
    return {"content": answer, "tool_use": [], "stop_reason": "end_turn", "usage": {}}


def _mock_policy(answer: str = "See policy."):
    import json
    return {"content": json.dumps({"answer": answer}), "tool_use": [], "stop_reason": "end_turn", "usage": {}}


def _mock_respond(text: str = "Done."):
    return {"content": text, "tool_use": [], "stop_reason": "end_turn", "usage": {}}


def _build_mock_side_effects(expected_tools: list[str]) -> list[dict]:
    """
    Build LLM response list for mock mode:
      1. classify (interpretation_node)
      2. select   (selection_node) — injects expected_tools
      3+. internal tool calls if needed (faq_lookup, policy_query, fetch_payment_history)

    Read tools that require specific fields must have stub inputs so Pydantic validation
    passes at execution time (faq_lookup/policy_query need a non-empty `question`).
    Action tools (process_refund, cancel_subscription, etc.) take their account_id from
    run_agent's kwarg, so tool_input={} is fine for them.
    """
    _STUB_INPUTS: dict[str, dict] = {
        "faq_lookup":    {"question": "mock question"},
        "policy_query":  {"question": "mock question"},
        "clarify":       {},
    }
    tool_entries = [
        {"tool_name": t, "tool_input": _STUB_INPUTS.get(t, {}), "justification": "eval mock"}
        for t in expected_tools
    ]
    responses: list[dict] = [
        _mock_classify("eval request"),
        _mock_select(tool_entries),
    ]
    # Add stub responses for tools that call LLM internally
    for tool in expected_tools:
        if tool == "faq_lookup":
            responses.append(_mock_faq("Mocked FAQ answer."))
        elif tool == "policy_query":
            responses.append(_mock_policy("Mocked policy answer."))
        elif tool == "fetch_payment_history":
            responses.append(_mock_respond("Mocked payment history."))
    return responses


# ── Policy outcome classifier ─────────────────────────────────────────────────

def _classify_outcome(resp: Any, expected: str) -> bool:
    """
    Map observed (halt_reason, error) → policy outcome label, compare to expected.

    Labels:
      eligible              — action tool reached confirmation_pending (policy passed)
      answered              — read tool succeeded
      form_collected        — form_required (policy will be checked on submit)
      blocked_*             — various block conditions
      conflict_detected     — ambiguity from conflict
      clarification_requested — general ambiguity
    """
    halt = resp.halt_reason
    error = resp.error or {}
    error_code = error.get("code", "") if isinstance(error, dict) else ""

    # Map halt reason + error code → policy label
    if halt == "confirmation_pending":
        observed = "eligible"
    elif halt == "success":
        observed = "answered"
    elif halt == "form_required":
        observed = "form_collected"
    elif halt == "ambiguity":
        if "conflict" in (resp.result or "").lower():
            observed = "conflict_detected"
        else:
            observed = "clarification_requested"
    elif halt == "tool_failure":
        mapping = {
            "REFUND_WINDOW_EXPIRED":        "blocked_expired_window",
            "REFUND_ESCALATION_REQUIRED":   "blocked_lifetime_limit",
            "PENDING_OPERATION_ACTIVE":     "blocked_pending_operation",
            "ALREADY_PAUSED":               "blocked_already_paused",
            "ALREADY_CANCELLED":            "blocked",
            "ACCOUNT_NOT_FOUND":            "blocked",
            "NO_ACTIVE_SUBSCRIPTION":       "blocked",
        }
        observed = mapping.get(error_code, "blocked")
    else:
        observed = halt or "unknown"

    return observed == expected


# ── Case runner ───────────────────────────────────────────────────────────────

def _run_case(case: dict, live: bool) -> dict:
    """Run a single eval case. Returns a result dict."""
    import uuid
    from core.agent import run_agent

    session_id = f"eval-{case['id']}-{uuid.uuid4().hex[:8]}"
    expected_tools: list[str] = case.get("expected_tools") or []
    expected_halt: str = case.get("expected_halt_reason", "")
    expected_policy: str = case.get("expected_policy_outcome", "")

    t0 = time.time()

    try:
        if live:
            resp = run_agent(
                message=case["message"],
                session_id=session_id,
                allow_history_reference=False,
                account_id=case.get("account_id"),
            )
            selected = _extract_selected_tools(resp)
        else:
            # Mock mode: inject expected_tools; measure halt_reason + policy only.
            # Both core.planner.llm_call and config.llm_call must be patched: planner.py
            # binds llm_call directly at import time (`from config import llm_call`), so
            # patching config alone does not intercept planner calls.
            side_effects = _build_mock_side_effects(expected_tools)
            mock = MagicMock(side_effect=side_effects)
            with patch("core.planner.llm_call", mock), patch("config.llm_call", mock):
                resp = run_agent(
                    message=case["message"],
                    session_id=session_id,
                    allow_history_reference=False,
                    account_id=case.get("account_id"),
                )
            selected = expected_tools  # routing is injected, not measured

    except Exception as exc:
        return {
            "id": case["id"],
            "passed": False,
            "routing_match": False,
            "halt_match": False,
            "policy_match": False,
            "error": str(exc),
            "latency_ms": int((time.time() - t0) * 1000),
            "halt_reason": "exception",
            "selected_tools": [],
            "expected_tools": expected_tools,
        }

    latency_ms = int((time.time() - t0) * 1000)

    routing_match = bool(live and set(selected) == set(expected_tools))
    halt_match = resp.halt_reason == expected_halt
    policy_match = _classify_outcome(resp, expected_policy)
    passed = halt_match and policy_match

    return {
        "id": case["id"],
        "passed": passed,
        "routing_match": routing_match,
        "halt_match": halt_match,
        "policy_match": policy_match,
        "error": None,
        "latency_ms": latency_ms,
        "halt_reason": resp.halt_reason,
        "expected_halt": expected_halt,
        "selected_tools": selected,
        "expected_tools": expected_tools,
        "notes": case.get("notes", ""),
    }


def _extract_selected_tools(resp: Any) -> list[str]:
    """Extract selected tool names from a RunResponse trace."""
    for step in resp.trace:
        if step.phase == "selection":
            data = step.data or {}
            return data.get("selected_tools") or []
    return []


# ── Scorecard printer ─────────────────────────────────────────────────────────

def _print_scorecard(results: list[dict], live: bool, verbose: bool) -> None:
    n = len(results)
    if n == 0:
        print("No cases run.")
        return

    exceptions  = [r for r in results if r.get("error")]
    passed      = [r for r in results if r["passed"]]
    halt_ok     = [r for r in results if r["halt_match"]]
    policy_ok   = [r for r in results if r["policy_match"]]
    escalations = [r for r in results if r["halt_reason"] in ("ambiguity", "max_iterations")]
    deflected   = [r for r in results if r["halt_reason"] in (
        "success", "confirmation_pending", "form_required", "cancelled",
    )]

    latencies = [r["latency_ms"] for r in results if r["latency_ms"] > 0]
    p50 = sorted(latencies)[len(latencies) // 2] if latencies else 0
    p95 = sorted(latencies)[int(len(latencies) * 0.95)] if latencies else 0

    print()
    print("=" * 60)
    print("  EVAL SCORECARD")
    print(f"  Mode:   {'live (real LLM)' if live else 'mock (LLM bypassed)'}")
    print(f"  Cases:  {n}  |  Exceptions: {len(exceptions)}")
    print("=" * 60)
    print(f"  Overall pass rate          {len(passed):>3}/{n}  ({100*len(passed)/n:.0f}%)")
    print(f"  Halt-reason accuracy       {len(halt_ok):>3}/{n}  ({100*len(halt_ok)/n:.0f}%)")
    print(f"  Policy-outcome accuracy    {len(policy_ok):>3}/{n}  ({100*len(policy_ok)/n:.0f}%)")
    if live:
        routing_ok = [r for r in results if r["routing_match"]]
        print(f"  Routing accuracy (live)    {len(routing_ok):>3}/{n}  ({100*len(routing_ok)/n:.0f}%)")
    print(f"  Escalation/ambiguity rate  {len(escalations):>3}/{n}  ({100*len(escalations)/n:.0f}%)")
    print(f"  Deflection rate            {len(deflected):>3}/{n}  ({100*len(deflected)/n:.0f}%)")
    print(f"  Latency p50/p95            {p50} ms / {p95} ms")
    print("-" * 60)

    if verbose or len(passed) < n:
        failures = [r for r in results if not r["passed"]]
        print(f"\n  {'FAILURES' if failures else 'ALL PASSED'} ({len(failures)} failed)")
        for r in failures:
            prefix = "  [EXC]" if r.get("error") else "  [FAIL]"
            print(f"{prefix} {r['id']}")
            if r.get("error"):
                print(f"         exception: {r['error']}")
            else:
                if not r["halt_match"]:
                    print(f"         halt:   expected={r.get('expected_halt','?')}  got={r['halt_reason']}")
                if not r["policy_match"]:
                    print(f"         policy: expected={r.get('notes','?')[:60]}")
                if live and not r["routing_match"]:
                    print(f"         tools:  expected={r['expected_tools']}  got={r['selected_tools']}")

    if verbose:
        print(f"\n  PER-CASE RESULTS:")
        for r in results:
            status = "PASS" if r["passed"] else "FAIL"
            print(f"  [{status}] {r['id']:<45} halt={r['halt_reason']:<25} {r['latency_ms']}ms")

    print("=" * 60)
    print()


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> int:
    # Ensure UTF-8 output on Windows (default console is cp1252).
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Run agent eval corpus.")
    parser.add_argument("--live", action="store_true", help="Use real LLM (requires ANTHROPIC_API_KEY)")
    parser.add_argument("--filter", default="", help="Only run cases whose id contains this string")
    parser.add_argument("--verbose", "-v", action="store_true", help="Print per-case results")
    args = parser.parse_args()

    corpus_path = Path(__file__).parent / "corpus.yaml"
    with open(corpus_path, encoding="utf-8") as f:
        corpus = yaml.safe_load(f)
    cases = corpus.get("cases") or []

    if args.filter:
        cases = [c for c in cases if args.filter in c["id"]]
        print(f"Filter '{args.filter}': {len(cases)} cases selected.")

    if not cases:
        print("No cases to run.")
        return 0

    print(f"\nRunning {len(cases)} eval cases in {'live' if args.live else 'mock'} mode...")

    # Set up isolated DB and tools
    tmp = _setup_eval_db()
    _setup_tools()

    results = []
    for i, case in enumerate(cases, 1):
        sys.stdout.write(f"\r  [{i:>3}/{len(cases)}] {case['id']:<50}")
        sys.stdout.flush()
        result = _run_case(case, live=args.live)
        results.append(result)

    _print_scorecard(results, live=args.live, verbose=args.verbose)

    failures = [r for r in results if not r["passed"]]
    rc = 0 if not failures else 1

    try:
        tmp.cleanup()
    except Exception:
        pass  # Windows: SQLite file may still be held; non-fatal

    return rc


if __name__ == "__main__":
    sys.exit(main())
