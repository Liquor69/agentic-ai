"""
core/memory.py

Conversation history management.

Per-session history is stored in the DB (db/crud) and retrieved on each request.
The allow_history_reference toggle (passed from RunRequest) controls whether
prior turns are injected into the LLM context for a given call.

Extension path (noted in ARCHITECTURE.md):
  Persistent cross-session retrieval (RAG-style) can be layered in here
  without touching core/agent.py or core/planner.py.
"""

from __future__ import annotations

from db import crud


def get_history(session_id: str) -> list[dict]:
    """Return the full turn history for a session, or [] if none exists."""
    return crud.get_session_history(session_id)


def save_history(session_id: str, history: list[dict]) -> None:
    """Overwrite the stored history for a session."""
    crud.upsert_session(session_id, history)


def append_turn(session_id: str, role: str, content: str) -> list[dict]:
    """
    Append one turn to the session history and persist it.
    Returns the updated history list.
    """
    history = crud.get_session_history(session_id)
    history.append({"role": role, "content": content})
    crud.upsert_session(session_id, history)
    return history


def clear(session_id: str) -> None:
    """Delete the history for a session."""
    crud.delete_session(session_id)


def build_context(session_id: str, allow_history_reference: bool) -> list[dict]:
    """
    Return the message list to prepend to the current LLM call.

    When allow_history_reference=False the call is stateless — returns [].
    When True, returns all prior turns so the LLM has full session context.
    The current user message is NOT included here; the agent appends it separately.
    """
    if not allow_history_reference:
        return []
    return crud.get_session_history(session_id)
