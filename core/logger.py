"""
core/logger.py

Two responsibilities:
  1. Configure Python's standard logging system (called once at startup via setup_logging()).
  2. Provide log_run() — persists a structured agent run entry to the DB via db/crud.

Nothing in this file makes routing or tool decisions.
"""

from __future__ import annotations

import logging
import logging.config
from typing import Any

from config import settings


# ─── Python logging setup ─────────────────────────────────────────────────────

def setup_logging() -> None:
    """
    Configure the root logger. Called once from main.py on startup.
    Format: timestamp | level | module | message.
    Level is read from LOG_LEVEL in .env (default: INFO).
    """
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    logging.config.dictConfig({
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "standard": {
                "format": "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "standard",
                "stream": "ext://sys.stdout",
            },
        },
        "root": {
            "level": level,
            "handlers": ["console"],
        },
    })


# ─── Structured run logger ────────────────────────────────────────────────────

def log_run(
    session_id: str,
    input: str,
    *,
    intent: str | None = None,
    selected_tool: str | None = None,
    reasoning: str | None = None,
    available_tools: list[str] | None = None,
    selection_justification: str | None = None,
    expected_result: str | None = None,
    constraint_validation: dict[str, Any] | None = None,
    tool_result: Any = None,
    final_response: str | None = None,
    error: dict[str, Any] | None = None,
    iterations_used: int | None = None,
    halt_reason: str | None = None,
    trace: list[dict[str, Any]] | None = None,
) -> int:
    """
    Persist one complete agent run to the database and emit a structured log line.

    All fields after session_id and input are keyword-only so callers are explicit.
    Returns the DB row id of the created log entry.

    Field source: ARCHITECTURE.md — agent_logs schema.
    Persistence: db/crud.write_log().
    """
    from db.crud import write_log  # imported here to avoid a circular import at module load

    _log = logging.getLogger(__name__)

    entry = write_log(
        session_id=session_id,
        input=input,
        intent=intent,
        selected_tool=selected_tool,
        reasoning=reasoning,
        available_tools=available_tools,
        selection_justification=selection_justification,
        expected_result=expected_result,
        constraint_validation=constraint_validation,
        tool_result=tool_result,
        final_response=final_response,
        error=error,
        iterations_used=iterations_used,
        halt_reason=halt_reason,
        trace=trace,
    )

    _log.info(
        "run logged | session=%s id=%s tool=%s halt=%s error=%s",
        session_id,
        entry.id,
        selected_tool or "—",
        halt_reason or "—",
        bool(error),
    )

    return entry.id
