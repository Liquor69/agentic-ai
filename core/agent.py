"""
core/agent.py

Entry point for the agent loop.

run_agent() initialises the LangGraph state, invokes the compiled graph,
and returns a RunResponse. It is the only function called by api/routes.py.

The graph itself lives in core/planner.py.
"""

from __future__ import annotations

import logging
import time
import traceback
import uuid
from typing import Any

from api.schemas import RunResponse, TraceStep
from config import get_token_usage, reset_token_counter, settings
from core.planner import AgentState, build_graph
from core.safety import InputRejected, validate_input
from db import crud

logger = logging.getLogger(__name__)


def run_agent(
    message: str,
    session_id: str | None,
    allow_history_reference: bool,
    confirmed: bool | None = None,
    account_id: str | None = None,
    form_data: dict | None = None,
    dry_run: bool = False,
    domain: str | None = None,
) -> RunResponse:
    """
    Run the agent loop for one request.

    Parameters
    ----------
    message                : Natural language input from the user.
    session_id             : Existing session to continue, or None to start a new one.
    allow_history_reference: When False, each call is stateless (no history injected).
    confirmed              : None = normal run.
                             True  = user confirmed a pending action (YES button).
                             False = user cancelled a pending action (NO button).
    form_data              : Form field values submitted when confirmed=True for a
                             form-type pending action. Merged into tool_input_data by
                             interpretation_node before routing to execution.
    dry_run                : When True, the full loop runs but tool execution is
                             simulated — no side effects. halt_reason = "dry_run".
    """
    if not session_id:
        session_id = str(uuid.uuid4())

    # ── Pre-graph input validation ─────────────────────────────────────────────
    # Only validate new requests (confirmed is None).
    # Confirmation responses (confirmed=True/False) are routing signals, not user text —
    # their message field is often empty and that is expected.
    t0 = time.perf_counter()
    reset_token_counter()
    if confirmed is None:
        try:
            message = validate_input(message, max_length=settings.max_input_length)
        except InputRejected as exc:
            latency_ms = int((time.perf_counter() - t0) * 1000)
            return RunResponse(
                result=exc.message,
                trace=[TraceStep(phase="safety", data={"code": exc.code, "message": exc.message})],
                session_id=session_id,
                iterations_used=0,
                halt_reason="input_rejected",
                error={"code": exc.code, "message": exc.message},
                latency_ms=latency_ms,
                token_usage={},
                dry_run=dry_run,
            )

    graph = build_graph()

    initial: AgentState = {
        # ── Input ──────────────────────────────────────────────────────────────
        "input": message,
        "session_id": session_id,
        "allow_history_reference": allow_history_reference,
        "confirmed": confirmed,
        "account_id": account_id,
        "account_context": None,
        "form_data": form_data,
        "dry_run": dry_run,
        "domain": domain or settings.domain_pack,
        # ── Interpretation ─────────────────────────────────────────────────────
        "classification": None,
        # ── Selection ──────────────────────────────────────────────────────────
        "intent": None,
        "selected_tool": None,
        "available_tools": [],
        "selection_justification": None,
        "tool_input_data": None,
        "form_spec": None,
        # ── Confirmation ───────────────────────────────────────────────────────
        "confirmation_payload": None,
        "confirmation_value": None,
        # ── Execution ──────────────────────────────────────────────────────────
        "selected_tools": None,
        "tool_result": None,
        "tool_results": [],
        "verified_changes": None,
        # ── Control ────────────────────────────────────────────────────────────
        "error": None,
        "iterations": 0,
        "halt_reason": None,
        "clarifying_question": None,
        # ── Session / output ───────────────────────────────────────────────────
        "history": [],
        "final_response": None,
        "trace": [],
    }

    try:
        final = graph.invoke(initial)
    except Exception as exc:
        tb = traceback.format_exc()
        logger.error("graph.invoke crashed | session=%s | %s\n%s", session_id, exc, tb)
        latency_ms = int((time.perf_counter() - t0) * 1000)
        return RunResponse(
            result="An internal error occurred. Please try again or contact support.",
            trace=[TraceStep(phase="error", data={"exception": str(exc), "traceback": tb})],
            session_id=session_id,
            iterations_used=0,
            halt_reason="internal_error",
            error={"code": "INTERNAL_ERROR", "message": str(exc), "traceback": tb},
            latency_ms=latency_ms,
            token_usage=get_token_usage(),
            dry_run=dry_run,
        )

    latency_ms = int((time.perf_counter() - t0) * 1000)
    token_usage = get_token_usage()

    # Patch the persisted log entry with real latency + token counts.
    # The log_id lives in the "log" phase trace step written by log_node.
    log_id = _extract_log_id(final)
    if log_id is not None:
        try:
            crud.update_log_metrics(log_id, latency_ms=latency_ms, token_usage=token_usage)
        except Exception:
            logger.warning("update_log_metrics failed for log_id=%s", log_id, exc_info=True)

    return RunResponse(
        result=final.get("final_response") or "",
        trace=[
            TraceStep(phase=s["phase"], data=s["data"])
            for s in (final.get("trace") or [])
        ],
        session_id=session_id,
        iterations_used=final.get("iterations") or 1,
        halt_reason=final.get("halt_reason"),
        error=final.get("error"),
        form_spec=final.get("form_spec"),
        latency_ms=latency_ms,
        token_usage=token_usage,
        dry_run=dry_run,
    )


def _extract_log_id(state: dict[str, Any]) -> int | None:
    """
    Pull the log row id from the 'log' phase trace step.
    log_node stores it as data["log_id"] in the trace.
    """
    for step in reversed(state.get("trace") or []):
        if step.get("phase") == "log":
            lid = step.get("data", {}).get("log_id")
            return int(lid) if lid is not None else None
    return None
