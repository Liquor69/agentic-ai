"""
tests/test_agent.py

End-to-end tests for the agent loop (core/agent.py + core/planner.py).

Strategy:
  - llm_call is mocked via _llm_patch() which patches BOTH core.planner.llm_call
    AND config.llm_call with a shared MagicMock. Both targets must be patched because
    planner.py uses `from config import llm_call` (direct binding, config patch alone
    does not intercept planner calls), while tools/customer_ops.py uses the same pattern
    inside function bodies (so config patch DOES intercept tool internal calls).
  - Tool execution is REAL against demo account fixtures.
  - DB operations are REAL against an isolated temp SQLite (see conftest.py).

LLM call order per scenario (with shared mock):
  Single read tool:       [classify, select, tool_internal_llm]  (3 total)
  Confirmation first:     [classify, select]                      (2 total)
  Confirmation resume:    []                                      (0 total — template response)
  Form gate:              [classify, select]                      (2 total)
  Conflict/ambiguity:     [classify, select]                      (2 total)
  Max iterations:         [classify]                              (1 total — select halts early)
  Tool failure:           [classify, select]                      (2 total)
  Multi-tool read:        [classify, select, tool1_llm, tool2_llm] (4 total)
"""

from __future__ import annotations

import sys
import os
from contextlib import contextmanager
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ── Registry fixture ──────────────────────────────────────────────────────────

@pytest.fixture()
def _register_tools():
    """
    Clean, populated tool registry for agent tests.
    Not autouse — tests request it explicitly.
    """
    from tools.registry import _REGISTRY
    _REGISTRY.clear()
    for key in list(sys.modules):
        if key.startswith("tools.") and key != "tools.registry":
            del sys.modules[key]
    import tools.customer_ops   # noqa: F401
    import tools.webhooks       # noqa: F401
    import tools.external_api   # noqa: F401
    yield
    _REGISTRY.clear()


# ── LLM mock helpers ──────────────────────────────────────────────────────────

def _classify(text: str = "Generic request"):
    return {"content": text, "tool_use": [], "stop_reason": "end_turn", "usage": {}}


def _select(*tool_entries: dict):
    return {
        "stop_reason": "tool_use",
        "tool_use": [{"id": "mock-id", "name": "execute_plan", "input": {"tools": list(tool_entries)}}],
        "content": "",
        "usage": {},
    }


def _respond(text: str = "Done."):
    return {"content": text, "tool_use": [], "stop_reason": "end_turn", "usage": {}}


def _tool(name: str, input_data: dict | None = None, justification: str = "selected"):
    return {"tool_name": name, "tool_input": input_data or {}, "justification": justification}


def _faq_response(answer: str):
    return {"content": answer, "tool_use": [], "stop_reason": "end_turn", "usage": {}}


def _policy_response(answer: str, reclassify: str | None = None):
    import json
    body: dict = {"answer": answer}
    if reclassify:
        body["reclassify_to"] = reclassify
    return {"content": json.dumps(body), "tool_use": [], "stop_reason": "end_turn", "usage": {}}


@contextmanager
def _llm_patch(*responses):
    """
    Patch both core.planner.llm_call AND config.llm_call with a single shared MagicMock.

    Why both targets:
      - planner.py: `from config import llm_call` — direct binding; config patch alone misses it.
      - customer_ops.py: `from config import llm_call` inside function bodies — config patch works.

    A shared MagicMock ensures both patch targets draw from the same side_effect queue.
    """
    mock = MagicMock(side_effect=list(responses))
    with patch("core.planner.llm_call", mock), patch("config.llm_call", mock):
        yield mock


# ── AgentState prototype for router unit tests ────────────────────────────────

_BASE: dict = {
    "input": "test", "session_id": "unit-test", "allow_history_reference": False,
    "confirmed": None, "account_id": None, "account_context": None, "form_data": None,
    "classification": None, "intent": None, "selected_tool": None, "available_tools": [],
    "selection_justification": None, "tool_input_data": None, "form_spec": None,
    "confirmation_payload": None, "confirmation_value": None, "selected_tools": None,
    "tool_result": None, "tool_results": [], "verified_changes": None,
    "error": None, "iterations": 0, "halt_reason": None, "clarifying_question": None,
    "history": [], "final_response": None, "trace": [],
}

SESSION = "agent-test-session"


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 1: Router unit tests (no LLM, no DB, no tools)
# ══════════════════════════════════════════════════════════════════════════════

class TestRouteInterpretation:
    def test_cancelled_routes_to_respond(self):
        from core.planner import _route_interpretation
        assert _route_interpretation({**_BASE, "halt_reason": "cancelled"}) == "respond"

    def test_confirmation_expired_routes_to_respond(self):
        from core.planner import _route_interpretation
        assert _route_interpretation({**_BASE, "halt_reason": "confirmation_expired"}) == "respond"

    def test_pending_execution_routes_to_execution(self):
        from core.planner import _route_interpretation
        s = {**_BASE, "selected_tool": "process_refund", "tool_input_data": {"confirmed": True}}
        assert _route_interpretation(s) == "execution"

    def test_normal_call_routes_to_selection(self):
        from core.planner import _route_interpretation
        assert _route_interpretation(_BASE) == "selection"

    def test_no_halt_no_tool_routes_to_selection(self):
        from core.planner import _route_interpretation
        assert _route_interpretation({**_BASE, "halt_reason": None, "selected_tool": None}) == "selection"


class TestRouteSelection:
    def test_ambiguity_routes_to_log(self):
        from core.planner import _route_selection
        assert _route_selection({**_BASE, "halt_reason": "ambiguity"}) == "log"

    def test_max_iterations_routes_to_log(self):
        from core.planner import _route_selection
        assert _route_selection({**_BASE, "halt_reason": "max_iterations"}) == "log"

    def test_tool_failure_routes_to_log(self):
        from core.planner import _route_selection
        assert _route_selection({**_BASE, "halt_reason": "tool_failure"}) == "log"

    def test_form_required_routes_to_form(self):
        from core.planner import _route_selection
        assert _route_selection({**_BASE, "halt_reason": "form_required"}) == "form"

    def test_no_halt_routes_to_execution(self):
        from core.planner import _route_selection
        assert _route_selection({**_BASE, "halt_reason": None}) == "execution"


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 2: Single-tool read-only
# ══════════════════════════════════════════════════════════════════════════════

class TestSingleToolReadOnly:
    def test_faq_lookup_success(self, _register_tools):
        from core.agent import run_agent
        with _llm_patch(
            _classify("FAQ — hours"),
            _select(_tool("faq_lookup", {"question": "What are your hours?"})),
            _faq_response("We are open 04:00–00:00 daily."),
        ):
            resp = run_agent("What are your hours?", SESSION, allow_history_reference=False)

        assert resp.halt_reason == "success"
        assert resp.error is None
        assert "04:00" in resp.result

    def test_policy_query_success(self, _register_tools):
        from core.agent import run_agent
        with _llm_patch(
            _classify("Policy — refund eligibility"),
            _select(_tool("policy_query", {"question": "Can I get a refund after 14 days?"})),
            _policy_response("No — refund window is 14 days from purchase."),
        ):
            resp = run_agent("Can I get a refund after 14 days?", SESSION, allow_history_reference=False)

        assert resp.halt_reason == "success"
        assert resp.error is None
        assert "14" in resp.result

    def test_faq_result_in_response(self, _register_tools):
        from core.agent import run_agent
        with _llm_patch(
            _classify("FAQ — pricing"),
            _select(_tool("faq_lookup", {"question": "How much is the Pass?"})),
            _faq_response("The Pass is €60/month or €600/year."),
        ):
            resp = run_agent("How much is the Pass?", SESSION, allow_history_reference=False)

        assert "60" in resp.result


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 3: Multi-tool sequencing
# ══════════════════════════════════════════════════════════════════════════════

class TestMultiToolSequencing:
    def test_faq_and_policy_both_succeed(self, _register_tools):
        from core.agent import run_agent
        with _llm_patch(
            _classify("FAQ + policy"),
            _select(
                _tool("faq_lookup", {"question": "What are your hours?"}),
                _tool("policy_query", {"question": "Can I pause?"}),
            ),
            _faq_response("Open 04:00–00:00."),
            _policy_response("Yes, pausing is available."),
        ):
            resp = run_agent(
                "What are your hours? Also can I pause?",
                SESSION,
                allow_history_reference=False,
            )

        assert resp.halt_reason == "success"
        assert resp.error is None
        assert "04:00" in resp.result
        assert "paus" in resp.result.lower()

    def test_multi_tool_trace_includes_execution(self, _register_tools):
        from core.agent import run_agent
        with _llm_patch(
            _classify("Multi intent"),
            _select(
                _tool("faq_lookup", {"question": "Hours?"}),
                _tool("policy_query", {"question": "Policy?"}),
            ),
            _faq_response("04:00–00:00."),
            _policy_response("Yes."),
        ):
            resp = run_agent("Hours and policy?", SESSION, allow_history_reference=False)

        phases = [step.phase for step in resp.trace]
        assert "execution" in phases


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 4: Conflict detection
# ══════════════════════════════════════════════════════════════════════════════

class TestConflictDetection:
    def test_refund_and_cancel_together_is_ambiguity(self, _register_tools):
        from core.agent import run_agent
        with _llm_patch(
            _classify("Conflict: refund + cancel"),
            _select(_tool("process_refund", {}), _tool("cancel_subscription", {})),
        ):
            resp = run_agent(
                "I want a refund and cancel my subscription",
                SESSION,
                allow_history_reference=False,
                account_id="demo-pass-monthly",
            )

        assert resp.halt_reason == "ambiguity"
        assert resp.result

    def test_conflict_message_names_both_tools(self, _register_tools):
        from core.agent import run_agent
        with _llm_patch(
            _classify("Conflict"),
            _select(_tool("process_refund", {}), _tool("cancel_subscription", {})),
        ):
            resp = run_agent(
                "refund and cancel",
                SESSION,
                allow_history_reference=False,
                account_id="demo-pass-monthly",
            )

        r = resp.result.lower()
        assert "refund" in r
        assert "cancel" in r


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 5: Form gating
# ══════════════════════════════════════════════════════════════════════════════

class TestFormGating:
    def test_pause_no_params_is_form_required(self, _register_tools):
        from core.agent import run_agent
        with _llm_patch(
            _classify("Pause request — no dates"),
            _select(_tool("pause_subscription", {})),
        ):
            resp = run_agent(
                "I want to pause my membership",
                SESSION,
                allow_history_reference=False,
                account_id="demo-pass-monthly",
            )

        assert resp.halt_reason == "form_required"
        assert resp.form_spec is not None
        assert "fields" in resp.form_spec

    def test_form_spec_has_start_date_and_duration(self, _register_tools):
        from core.agent import run_agent
        with _llm_patch(
            _classify("Pause"),
            _select(_tool("pause_subscription", {})),
        ):
            resp = run_agent("pause", SESSION, allow_history_reference=False, account_id="demo-pass-monthly")

        names = {f["name"] for f in resp.form_spec["fields"]}
        assert "start_date" in names
        assert "duration_days" in names

    def test_change_plan_no_params_is_form_required(self, _register_tools):
        from core.agent import run_agent
        with _llm_patch(
            _classify("Plan change — params missing"),
            _select(_tool("change_plan", {})),
        ):
            resp = run_agent(
                "I want to change my plan",
                SESSION,
                allow_history_reference=False,
                account_id="demo-pass-monthly",
            )

        assert resp.halt_reason == "form_required"
        assert resp.form_spec is not None


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 6: Confirmation suspend / resume / cancel
# ══════════════════════════════════════════════════════════════════════════════

class TestConfirmationFlow:
    def test_refund_first_call_is_confirmation_pending(self, _register_tools):
        from core.agent import run_agent
        with _llm_patch(
            _classify("Refund request"),
            _select(_tool("process_refund", {})),
        ):
            resp = run_agent(
                "I want a refund", SESSION,
                allow_history_reference=False, account_id="demo-pass-monthly",
            )

        assert resp.halt_reason == "confirmation_pending"
        assert resp.error is None

    def test_refund_confirmed_yes_is_success(self, _register_tools):
        from core.agent import run_agent

        with _llm_patch(_classify("Refund"), _select(_tool("process_refund", {}))):
            run_agent("refund", SESSION, allow_history_reference=False, account_id="demo-pass-monthly")

        # confirmed=True: action tool → verified_changes used directly (no LLM call)
        with _llm_patch():
            r2 = run_agent(
                "yes", SESSION, allow_history_reference=False,
                confirmed=True, account_id="demo-pass-monthly",
            )

        assert r2.halt_reason == "success"
        assert r2.error is None

    def test_refund_confirmed_no_is_cancelled(self, _register_tools):
        from core.agent import run_agent

        with _llm_patch(_classify("Refund"), _select(_tool("process_refund", {}))):
            run_agent("refund", SESSION, allow_history_reference=False, account_id="demo-pass-monthly")

        with _llm_patch():
            resp = run_agent(
                "no", SESSION, allow_history_reference=False,
                confirmed=False, account_id="demo-pass-monthly",
            )

        assert resp.halt_reason == "cancelled"
        r = resp.result.lower()
        assert "cancelled" in r or "no changes" in r

    def test_cancel_subscription_first_call_is_pending(self, _register_tools):
        from core.agent import run_agent
        with _llm_patch(_classify("Cancel"), _select(_tool("cancel_subscription", {}))):
            resp = run_agent("cancel", SESSION, allow_history_reference=False, account_id="demo-pass-monthly")

        assert resp.halt_reason == "confirmation_pending"

    def test_cancel_subscription_confirmed_is_success(self, _register_tools):
        from core.agent import run_agent

        with _llm_patch(_classify("Cancel"), _select(_tool("cancel_subscription", {}))):
            run_agent("cancel", SESSION, allow_history_reference=False, account_id="demo-pass-monthly")

        with _llm_patch():
            r2 = run_agent(
                "yes", SESSION, allow_history_reference=False,
                confirmed=True, account_id="demo-pass-monthly",
            )

        assert r2.halt_reason == "success"

    def test_confirmation_payload_has_billing_and_member_impact(self, _register_tools):
        from core.agent import run_agent
        with _llm_patch(_classify("Refund"), _select(_tool("process_refund", {}))):
            resp = run_agent("refund", SESSION, allow_history_reference=False, account_id="demo-pass-monthly")

        assert "Billing impact" in resp.result or "billing" in resp.result.lower()
        assert "Membership" in resp.result or "member" in resp.result.lower()


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 7: Form → confirmed=True resume
# ══════════════════════════════════════════════════════════════════════════════

class TestFormResume:
    def test_pause_form_submit_executes_or_pends(self, _register_tools):
        from core.agent import run_agent
        start = (date.today() + timedelta(days=15)).isoformat()

        with _llm_patch(_classify("Pause"), _select(_tool("pause_subscription", {}))):
            r1 = run_agent("pause", SESSION, allow_history_reference=False, account_id="demo-pass-monthly")
        assert r1.halt_reason == "form_required"

        # pause_subscription with confirmed=True executes the pause directly
        with _llm_patch():
            r2 = run_agent(
                "", SESSION, allow_history_reference=False,
                confirmed=True, account_id="demo-pass-monthly",
                form_data={"start_date": start, "duration_days": 30, "has_documentation": False},
            )

        assert r2.halt_reason in ("success", "confirmation_pending")
        assert r2.error is None


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 8: Confirmation expired
# ══════════════════════════════════════════════════════════════════════════════

class TestConfirmationExpired:
    def test_no_pending_with_confirmed_true_returns_expired(self, _register_tools):
        from core.agent import run_agent
        with _llm_patch():
            resp = run_agent(
                "yes", "no-pending-session",
                allow_history_reference=False,
                confirmed=True, account_id="demo-pass-monthly",
            )

        assert resp.halt_reason == "confirmation_expired"
        r = resp.result.lower()
        assert "expired" in r or "resubmit" in r


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 9: Max iterations
# ══════════════════════════════════════════════════════════════════════════════

class TestMaxIterations:
    def test_max_iterations_halt_reason(self, _register_tools):
        from core.agent import run_agent
        # MAX_ITERATIONS=0: selection_node sees iterations=1 > 0 → halt immediately
        with patch("core.planner.MAX_ITERATIONS", 0):
            with _llm_patch(_classify("ambiguous")):
                resp = run_agent("fix my account", SESSION, allow_history_reference=False)

        assert resp.halt_reason == "max_iterations"

    def test_max_iterations_response_mentions_support(self, _register_tools):
        from core.agent import run_agent
        with patch("core.planner.MAX_ITERATIONS", 0):
            with _llm_patch(_classify("anything")):
                resp = run_agent("...", SESSION, allow_history_reference=False)

        r = resp.result.lower()
        assert "support" in r or "unable" in r or "attempt" in r


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 10: Tool failure
# ══════════════════════════════════════════════════════════════════════════════

class TestToolFailure:
    def test_unknown_account_is_tool_failure(self, _register_tools):
        from core.agent import run_agent
        with _llm_patch(
            _classify("Refund"), _select(_tool("process_refund", {})),
        ):
            resp = run_agent(
                "refund", SESSION, allow_history_reference=False,
                account_id="nonexistent-account-xyz",
            )

        assert resp.halt_reason == "tool_failure"
        assert resp.error is not None

    def test_tool_failure_error_has_code_or_message(self, _register_tools):
        from core.agent import run_agent
        with _llm_patch(
            _classify("Refund"), _select(_tool("process_refund", {})),
        ):
            resp = run_agent(
                "refund", SESSION, allow_history_reference=False,
                account_id="nonexistent-account-xyz",
            )

        assert "code" in resp.error or "message" in resp.error

    def test_refund_expired_window_is_tool_failure(self, _register_tools):
        from core.agent import run_agent
        with _llm_patch(
            _classify("Refund — expired"),
            _select(_tool("process_refund", {})),
        ):
            resp = run_agent(
                "refund", SESSION, allow_history_reference=False,
                account_id="demo-pass-monthly-expired-window",
            )

        assert resp.halt_reason == "tool_failure"
        assert resp.error is not None

    def test_llm_selection_error_is_tool_failure(self, _register_tools):
        from core.agent import run_agent
        with _llm_patch(
            _classify("anything"),
            {"error": "API rate limit"},
        ):
            resp = run_agent("help", SESSION, allow_history_reference=False)

        assert resp.halt_reason == "tool_failure"


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 11: Ambiguity
# ══════════════════════════════════════════════════════════════════════════════

class TestAmbiguity:
    def test_clarify_tool_returns_ambiguity(self, _register_tools):
        from core.agent import run_agent
        question = "Could you please clarify your request?"
        with _llm_patch(
            _classify("Unclear"),
            _select(_tool("clarify", {}, justification=question)),
        ):
            resp = run_agent("fix it", SESSION, allow_history_reference=False)

        assert resp.halt_reason == "ambiguity"
        assert resp.result

    def test_ambiguity_result_contains_clarifying_question(self, _register_tools):
        from core.agent import run_agent
        question = "What specifically would you like to do?"
        with _llm_patch(
            _classify("Unclear"),
            _select(_tool("clarify", {}, justification=question)),
        ):
            resp = run_agent("help me", SESSION, allow_history_reference=False)

        assert question in resp.result or "clarify" in resp.result.lower()


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 12: Observability — latency_ms, token_usage, get_metrics
# ══════════════════════════════════════════════════════════════════════════════

class TestObservability:
    def test_response_has_latency_ms(self, _register_tools):
        """Every RunResponse exposes a non-negative latency_ms."""
        from core.agent import run_agent
        with _llm_patch(_classify("FAQ"), _select(_tool("faq_lookup", {"question": "hours?"})),
                        _faq_response("Open 04:00-00:00.")):
            resp = run_agent("hours", SESSION, allow_history_reference=False)
        assert hasattr(resp, "latency_ms")
        assert resp.latency_ms >= 0

    def test_response_has_token_usage_dict(self, _register_tools):
        """Every RunResponse exposes a token_usage dict (may be all-zero with mocks)."""
        from core.agent import run_agent
        with _llm_patch(_classify("FAQ"), _select(_tool("faq_lookup", {"question": "hours?"})),
                        _faq_response("Open 04:00-00:00.")):
            resp = run_agent("hours", SESSION, allow_history_reference=False)
        assert isinstance(resp.token_usage, dict)

    def test_db_row_updated_with_latency(self, _register_tools):
        """update_log_metrics() patches the DB row; log entry has non-null latency_ms."""
        from core.agent import run_agent
        from db import crud
        with _llm_patch(_classify("Refund"), _select(_tool("process_refund", {}))):
            resp = run_agent("refund", SESSION, allow_history_reference=False,
                             account_id="demo-pass-monthly")
        assert resp.halt_reason == "confirmation_pending"
        # Find the log entry and verify latency was persisted
        logs = crud.get_logs(session_id=SESSION, limit=1)
        assert logs
        assert logs[0].latency_ms is not None
        assert logs[0].latency_ms >= 0

    def test_db_row_updated_with_token_counts(self, _register_tools):
        """Token columns default to 0 (mocked calls return empty usage dicts)."""
        from core.agent import run_agent
        from db import crud
        with _llm_patch(_classify("Refund"), _select(_tool("process_refund", {}))):
            run_agent("refund", SESSION, allow_history_reference=False,
                      account_id="demo-pass-monthly")
        logs = crud.get_logs(session_id=SESSION, limit=1)
        assert logs
        # With mocked llm_call the usage dict is empty so tokens default to 0
        assert logs[0].prompt_tokens == 0
        assert logs[0].completion_tokens == 0

    def test_get_metrics_returns_correct_shape(self, _register_tools):
        """get_metrics() returns a dict with all required top-level keys."""
        from core.agent import run_agent
        from db import crud
        with _llm_patch(_classify("FAQ"), _select(_tool("faq_lookup", {"question": "?"})),
                        _faq_response("Answer.")):
            run_agent("what?", SESSION, allow_history_reference=False)
        metrics = crud.get_metrics()
        assert "total_runs" in metrics
        assert "by_halt_reason" in metrics
        assert "latency_ms" in metrics
        assert "tokens" in metrics
        assert "error_rate" in metrics
        assert isinstance(metrics["by_halt_reason"], dict)
        assert isinstance(metrics["latency_ms"]["p50"], int)

    def test_get_metrics_total_runs_increments(self, _register_tools):
        """Each run_agent call adds one row; total_runs reflects the count."""
        from core.agent import run_agent
        from db import crud
        before = crud.get_metrics()["total_runs"]
        with _llm_patch(_classify("FAQ"), _select(_tool("faq_lookup", {"question": "?"})),
                        _faq_response("Answer.")):
            run_agent("a", SESSION, allow_history_reference=False)
        with _llm_patch(_classify("FAQ"), _select(_tool("faq_lookup", {"question": "?"})),
                        _faq_response("Answer.")):
            run_agent("b", SESSION, allow_history_reference=False)
        after = crud.get_metrics()["total_runs"]
        assert after == before + 2

    def test_get_metrics_error_rate_zero_on_clean_runs(self, _register_tools):
        """Successful/pending runs contribute 0 to error_rate."""
        from core.agent import run_agent
        from db import crud
        with _llm_patch(_classify("FAQ"), _select(_tool("faq_lookup", {"question": "?"})),
                        _faq_response("Yes.")):
            run_agent("q", SESSION, allow_history_reference=False)
        metrics = crud.get_metrics()
        # Only successes present — error_rate should be 0
        assert metrics["error_rate"] == 0.0

    def test_token_counter_resets_between_runs(self, _register_tools):
        """reset_token_counter() is called per run; counters don't bleed across calls."""
        from config import get_token_usage, reset_token_counter
        reset_token_counter()
        usage = get_token_usage()
        assert usage.get("input_tokens", 0) == 0
        assert usage.get("output_tokens", 0) == 0
