"""
db/crud.py

Database read/write operations.

All functions accept an optional `db` session parameter.
When called without one they open and close their own session (fire-and-forget).
Callers that need transactional control pass their own session.

Never import tool or agent modules here — db/ has no upward dependencies.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any, Generator

from sqlalchemy.orm import Session

from config import settings
from db.models import AgentLog, PendingConfirmation, Session as SessionModel, SessionLocal


# ─── Session context manager ──────────────────────────────────────────────────

@contextmanager
def get_db() -> Generator[Session, None, None]:
    """Yield a SQLAlchemy session, committing on success and rolling back on error."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# ─── Agent logs ───────────────────────────────────────────────────────────────

def write_log(
    session_id: str,
    input: str,
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
    db: Session | None = None,
) -> AgentLog:
    """
    Persist a complete agent run log entry.
    JSON-serialisable fields are stored as text; None values are stored as NULL.
    """
    entry = AgentLog(
        session_id=session_id,
        timestamp=datetime.utcnow(),
        input=input,
        intent=intent,
        selected_tool=selected_tool,
        reasoning=reasoning,
        available_tools=json.dumps(available_tools) if available_tools is not None else None,
        selection_justification=selection_justification,
        expected_result=expected_result,
        constraint_validation=json.dumps(constraint_validation) if constraint_validation is not None else None,
        tool_result=json.dumps(tool_result, default=str) if tool_result is not None else None,
        final_response=final_response,
        error=json.dumps(error) if error is not None else None,
        iterations_used=iterations_used,
        halt_reason=halt_reason,
        trace=json.dumps(trace, default=str) if trace is not None else None,
    )

    if db is not None:
        db.add(entry)
        db.flush()
        return entry

    with get_db() as _db:
        _db.add(entry)
        _db.flush()
        return entry


def get_logs(
    session_id: str | None = None,
    intent: str | None = None,
    selected_tool: str | None = None,
    has_error: bool | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 100,
    offset: int = 0,
    db: Session | None = None,
) -> list[AgentLog]:
    """
    Query agent logs with optional filters.
    Supports all filter combinations defined in the GET /logs API contract.
    """
    def _query(session: Session) -> list[AgentLog]:
        q = session.query(AgentLog)
        if session_id:
            q = q.filter(AgentLog.session_id == session_id)
        if intent:
            q = q.filter(AgentLog.intent == intent)
        if selected_tool:
            q = q.filter(AgentLog.selected_tool == selected_tool)
        if has_error is True:
            q = q.filter(AgentLog.error.isnot(None))
        elif has_error is False:
            q = q.filter(AgentLog.error.is_(None))
        if since:
            q = q.filter(AgentLog.timestamp >= since)
        if until:
            q = q.filter(AgentLog.timestamp <= until)
        return q.order_by(AgentLog.timestamp.desc()).offset(offset).limit(limit).all()

    if db is not None:
        return _query(db)
    with get_db() as _db:
        return _query(_db)


def get_log_by_id(log_id: int, db: Session | None = None) -> AgentLog | None:
    def _query(session: Session) -> AgentLog | None:
        return session.query(AgentLog).filter(AgentLog.id == log_id).first()

    if db is not None:
        return _query(db)
    with get_db() as _db:
        return _query(_db)


# ─── Sessions (conversation history) ─────────────────────────────────────────

def get_session(session_id: str, db: Session | None = None) -> SessionModel | None:
    def _query(session: Session) -> SessionModel | None:
        return session.query(SessionModel).filter(SessionModel.session_id == session_id).first()

    if db is not None:
        return _query(db)
    with get_db() as _db:
        return _query(_db)


def upsert_session(
    session_id: str,
    history: list[dict[str, Any]],
    db: Session | None = None,
) -> SessionModel:
    """
    Create a new session or overwrite the history of an existing one.
    Updates last_active timestamp on every call.
    """
    def _upsert(session: Session) -> SessionModel:
        record = session.query(SessionModel).filter(SessionModel.session_id == session_id).first()
        now = datetime.utcnow()
        if record is None:
            record = SessionModel(
                session_id=session_id,
                created_at=now,
                last_active=now,
                history=json.dumps(history),
            )
            session.add(record)
        else:
            record.history = json.dumps(history)
            record.last_active = now
        session.flush()
        return record

    if db is not None:
        return _upsert(db)
    with get_db() as _db:
        return _upsert(_db)


def get_session_history(session_id: str, db: Session | None = None) -> list[dict[str, Any]]:
    """Returns the history list for a session, or [] if the session does not exist."""
    record = get_session(session_id, db=db)
    if record is None:
        return []
    try:
        return json.loads(record.history)
    except (json.JSONDecodeError, TypeError):
        return []


def delete_session(session_id: str, db: Session | None = None) -> None:
    def _delete(session: Session) -> None:
        session.query(SessionModel).filter(SessionModel.session_id == session_id).delete()

    if db is not None:
        _delete(db)
        return
    with get_db() as _db:
        _delete(_db)


def purge_expired_sessions(db: Session | None = None) -> int:
    """
    Delete sessions that have been inactive beyond SESSION_TTL_SECONDS.
    Returns the number of rows deleted.
    """
    cutoff = datetime.utcnow() - timedelta(seconds=settings.session_ttl_seconds)

    def _purge(session: Session) -> int:
        deleted = (
            session.query(SessionModel)
            .filter(SessionModel.last_active < cutoff)
            .delete(synchronize_session=False)
        )
        return deleted

    if db is not None:
        return _purge(db)
    with get_db() as _db:
        return _purge(_db)


# ─── Pending confirmations ────────────────────────────────────────────────────

def create_pending_confirmation(
    session_id: str,
    tool_name: str,
    payload: dict[str, Any],
    input_data: dict[str, Any],
    db: Session | None = None,
) -> PendingConfirmation:
    """
    Persist a confirmation suspended mid-run.
    Replaces any existing pending confirmation for the same session
    (only one pending action per session at a time).
    TTL is CONFIRMATION_TTL_SEC from config (default: 600 s / 10 min).
    """
    now = datetime.utcnow()
    expires = now + timedelta(seconds=settings.confirmation_ttl_seconds)

    def _create(session: Session) -> PendingConfirmation:
        # Clear any existing pending for this session
        session.query(PendingConfirmation).filter(
            PendingConfirmation.session_id == session_id
        ).delete(synchronize_session=False)

        record = PendingConfirmation(
            session_id=session_id,
            tool_name=tool_name,
            payload=json.dumps(payload),
            input_data=json.dumps(input_data),
            created_at=now,
            expires_at=expires,
            is_resolved=False,
        )
        session.add(record)
        session.flush()
        return record

    if db is not None:
        return _create(db)
    with get_db() as _db:
        return _create(_db)


def get_pending_confirmation(
    session_id: str,
    db: Session | None = None,
) -> PendingConfirmation | None:
    """
    Return the active pending confirmation for a session, or None if:
      - no pending confirmation exists
      - it has already been resolved
      - it has expired (TTL exceeded)
    """
    def _get(session: Session) -> PendingConfirmation | None:
        record = (
            session.query(PendingConfirmation)
            .filter(
                PendingConfirmation.session_id == session_id,
                PendingConfirmation.is_resolved == False,
                PendingConfirmation.expires_at > datetime.utcnow(),
            )
            .first()
        )
        return record

    if db is not None:
        return _get(db)
    with get_db() as _db:
        return _get(_db)


def resolve_pending_confirmation(session_id: str, db: Session | None = None) -> None:
    """Mark the pending confirmation for a session as resolved."""
    def _resolve(session: Session) -> None:
        session.query(PendingConfirmation).filter(
            PendingConfirmation.session_id == session_id
        ).update({"is_resolved": True}, synchronize_session=False)

    if db is not None:
        _resolve(db)
        return
    with get_db() as _db:
        _resolve(_db)


def cancel_pending_confirmation(session_id: str, db: Session | None = None) -> None:
    """
    Delete the pending confirmation for a session.
    Called when a new inbound message arrives while a confirmation is pending —
    per policy, the pending action is cancelled and the agent restarts from perceive.
    """
    def _cancel(session: Session) -> None:
        session.query(PendingConfirmation).filter(
            PendingConfirmation.session_id == session_id
        ).delete(synchronize_session=False)

    if db is not None:
        _cancel(db)
        return
    with get_db() as _db:
        _cancel(_db)


def get_pending_confirmation_data(
    session_id: str,
    db: Session | None = None,
) -> tuple[str, dict[str, Any], dict[str, Any]] | None:
    """
    Convenience wrapper: returns (tool_name, payload_dict, input_data_dict) or None.
    Callers that need the raw ORM object should use get_pending_confirmation() directly.
    """
    record = get_pending_confirmation(session_id, db=db)
    if record is None:
        return None
    return (
        record.tool_name,
        json.loads(record.payload),
        json.loads(record.input_data),
    )


def purge_expired_confirmations(db: Session | None = None) -> int:
    """Delete all expired, unresolved confirmations. Returns count deleted."""
    def _purge(session: Session) -> int:
        return (
            session.query(PendingConfirmation)
            .filter(PendingConfirmation.expires_at <= datetime.utcnow())
            .delete(synchronize_session=False)
        )

    if db is not None:
        return _purge(db)
    with get_db() as _db:
        return _purge(_db)
