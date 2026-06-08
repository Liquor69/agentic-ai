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
from db.models import AgentLog, DemoAccountState, PendingConfirmation, Session as SessionModel, SessionLocal


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
    latency_ms: int | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    cache_read_tokens: int | None = None,
    cache_creation_tokens: int | None = None,
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
        latency_ms=latency_ms,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_creation_tokens=cache_creation_tokens,
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


def update_log_metrics(
    log_id: int,
    latency_ms: int,
    token_usage: dict[str, int],
    db: Session | None = None,
) -> None:
    """
    Patch an existing agent_log row with timing and token data.
    Called from core/agent.py after graph.invoke() returns, when we know both
    the wall-clock latency and the accumulated LLM token counts for the run.
    """
    def _update(session: Session) -> None:
        session.query(AgentLog).filter(AgentLog.id == log_id).update(
            {
                "latency_ms":            latency_ms,
                "prompt_tokens":         token_usage.get("input_tokens", 0),
                "completion_tokens":     token_usage.get("output_tokens", 0),
                "cache_read_tokens":     token_usage.get("cache_read_input_tokens", 0),
                "cache_creation_tokens": token_usage.get("cache_creation_input_tokens", 0),
            },
            synchronize_session=False,
        )

    if db is not None:
        _update(db)
        return
    with get_db() as _db:
        _update(_db)


def get_metrics(
    since: datetime | None = None,
    until: datetime | None = None,
    db: Session | None = None,
) -> dict[str, Any]:
    """
    Aggregate stats from agent_logs for GET /metrics.

    Returns:
        period          — time range applied (ISO strings or null)
        total_runs      — total log entries in the range
        by_halt_reason  — count per halt_reason label
        latency_ms      — avg / p50 / p95 wall-clock latency (rows with latency_ms != null)
        tokens          — sum of prompt/completion/cache_read/cache_creation tokens
        error_rate      — (tool_failure + internal_error) / total_runs
    """
    from sqlalchemy import func

    def _compute(session: Session) -> dict[str, Any]:
        q = session.query(AgentLog)
        if since:
            q = q.filter(AgentLog.timestamp >= since)
        if until:
            q = q.filter(AgentLog.timestamp <= until)

        total = q.count()

        # Halt-reason breakdown
        halt_rows = (
            q.with_entities(AgentLog.halt_reason, func.count(AgentLog.id))
            .group_by(AgentLog.halt_reason)
            .all()
        )
        by_halt: dict[str, int] = {(r[0] or "unknown"): r[1] for r in halt_rows}

        # Token sums (NULL treated as 0)
        sums = q.with_entities(
            func.coalesce(func.sum(AgentLog.prompt_tokens), 0),
            func.coalesce(func.sum(AgentLog.completion_tokens), 0),
            func.coalesce(func.sum(AgentLog.cache_read_tokens), 0),
            func.coalesce(func.sum(AgentLog.cache_creation_tokens), 0),
        ).one()

        # Latency percentiles (Python-side sort; filtered to rows with data)
        lat_rows = (
            q.with_entities(AgentLog.latency_ms)
            .filter(AgentLog.latency_ms.isnot(None))
            .all()
        )
        latencies = sorted(r[0] for r in lat_rows)
        if latencies:
            n = len(latencies)
            avg_lat = round(sum(latencies) / n, 1)
            p50 = latencies[n // 2]
            p95 = latencies[min(n - 1, int(n * 0.95))]
        else:
            avg_lat = p50 = p95 = 0

        error_count = by_halt.get("tool_failure", 0) + by_halt.get("internal_error", 0)

        return {
            "period": {
                "since": since.isoformat() if since else None,
                "until": until.isoformat() if until else None,
            },
            "total_runs": total,
            "by_halt_reason": by_halt,
            "latency_ms": {"avg": avg_lat, "p50": p50, "p95": p95},
            "tokens": {
                "total_prompt":          int(sums[0]),
                "total_completion":      int(sums[1]),
                "total_cache_read":      int(sums[2]),
                "total_cache_creation":  int(sums[3]),
            },
            "error_rate": round(error_count / max(total, 1), 4),
        }

    if db is not None:
        return _compute(db)
    with get_db() as _db:
        return _compute(_db)


def count_successful_tool_calls(
    session_id: str,
    tool_name: str,
    db: Session | None = None,
) -> int:
    """
    Count how many times *tool_name* has been executed successfully in *session_id*.
    Used by core/safety.check_rate_limit() to enforce per-session action limits.

    Counts rows where selected_tool == tool_name AND halt_reason == "success".
    Multi-tool runs where a destructive action is not the primary (first) tool are
    conservatively under-counted — acceptable for a rate-limit guard.
    """
    def _count(session: Session) -> int:
        return (
            session.query(AgentLog)
            .filter(
                AgentLog.session_id == session_id,
                AgentLog.selected_tool == tool_name,
                AgentLog.halt_reason == "success",
            )
            .count()
        )

    if db is not None:
        return _count(db)
    with get_db() as _db:
        return _count(_db)


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


# ─── Demo account state ───────────────────────────────────────────────────────

def get_demo_account_state(account_id: str, db: Session | None = None) -> dict | None:
    """
    Return the persisted state dict for *account_id*, or None if no override exists.
    None means the fixture default should be used — not that the account is absent.
    """
    def _get(session: Session) -> dict | None:
        row = session.query(DemoAccountState).filter(
            DemoAccountState.account_id == account_id
        ).first()
        if row is None:
            return None
        try:
            return json.loads(row.state)
        except (json.JSONDecodeError, TypeError):
            return None

    if db is not None:
        return _get(db)
    with get_db() as _db:
        return _get(_db)


def set_demo_account_state(
    account_id: str,
    state_dict: dict,
    db: Session | None = None,
) -> None:
    """
    Upsert the full state dict for *account_id*.
    Overwrites any existing row; creates one if absent.
    """
    now = datetime.utcnow()

    def _set(session: Session) -> None:
        row = session.query(DemoAccountState).filter(
            DemoAccountState.account_id == account_id
        ).first()
        if row is None:
            row = DemoAccountState(
                account_id=account_id,
                state=json.dumps(state_dict, default=str),
                updated_at=now,
            )
            session.add(row)
        else:
            row.state = json.dumps(state_dict, default=str)
            row.updated_at = now
        session.flush()

    if db is not None:
        _set(db)
        return
    with get_db() as _db:
        _set(_db)


def reset_demo_account_state(account_id: str, db: Session | None = None) -> None:
    """
    Delete the DB override for *account_id*.
    After this, _get_account() will fall back to the fixture default.
    """
    def _reset(session: Session) -> None:
        session.query(DemoAccountState).filter(
            DemoAccountState.account_id == account_id
        ).delete(synchronize_session=False)

    if db is not None:
        _reset(db)
        return
    with get_db() as _db:
        _reset(_db)


def reset_all_demo_account_states(db: Session | None = None) -> None:
    """
    Delete all demo account overrides. Restores all accounts to fixture defaults.
    Called by POST /accounts/reset.
    """
    def _reset_all(session: Session) -> None:
        session.query(DemoAccountState).delete(synchronize_session=False)

    if db is not None:
        _reset_all(db)
        return
    with get_db() as _db:
        _reset_all(_db)
