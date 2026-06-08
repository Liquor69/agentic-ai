"""
core/agent.py

Entry point for the agent loop.

run_agent() initialises the LangGraph state, invokes the compiled graph,
and returns a RunResponse. It is the only function called by api/routes.py.

The graph itself lives in core/planner.py.
"""

from __future__ import annotations

import logging
import traceback
import uuid

from api.schemas import RunResponse, TraceStep
from core.planner import AgentState, build_graph

logger = logging.getLogger(__name__)


def run_agent(
    message: str,
    session_id: str | None,
    allow_history_reference: bool,
    confirmed: bool | None = None,
    account_id: str | None = None,
    form_data: dict | None = None,
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
    """
    if not session_id:
        session_id = str(uuid.uuid4())

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
        return RunResponse(
            result="An internal error occurred. Please try again or contact support.",
            trace=[TraceStep(phase="error", data={"exception": str(exc), "traceback": tb})],
            session_id=session_id,
            iterations_used=0,
            halt_reason="internal_error",
            error={"code": "INTERNAL_ERROR", "message": str(exc), "traceback": tb},
        )

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
    )
