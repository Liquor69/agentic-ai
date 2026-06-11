"""
db/models.py

SQLAlchemy table definitions.

Default engine: SQLite (zero setup, file-based).
Production swap: set DATABASE_URL=postgresql://... in .env — no code changes required.

Four tables:
  agent_logs            — one row per agent run, full decision trace
  sessions              — per-session conversation history for memory.py
  pending_confirmations — action confirmations awaiting member approval (TTL: 10 min)
  demo_account_states   — persisted demo account state (overrides fixture defaults)
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from config import settings

# ─── Engine & session factory ─────────────────────────────────────────────────

# connect_args is SQLite-specific; ignored by other drivers.
_connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}

engine = create_engine(settings.database_url, connect_args=_connect_args)

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False)


# ─── Base ─────────────────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    pass


# ─── Table 1: agent_logs ──────────────────────────────────────────────────────
# One row per agent run. Schema: ARCHITECTURE.md — agent_logs schema.
# JSON fields are stored as Text — SQLite has no native JSON type, and
# SQLAlchemy's JSON type maps correctly to JSONB on PostgreSQL.

class AgentLog(Base):
    __tablename__ = "agent_logs"

    id               = Column(Integer, primary_key=True, autoincrement=True)
    session_id       = Column(String(64),  nullable=False, index=True)
    timestamp        = Column(DateTime,    nullable=False, default=datetime.utcnow)

    # Perceive
    input            = Column(Text,        nullable=False)

    # Plan
    intent           = Column(String(128), nullable=True)
    selected_tool    = Column(String(128), nullable=True)
    reasoning        = Column(Text,        nullable=True)   # selection_justification

    # Extended plan fields (LangGraph state object)
    available_tools         = Column(Text, nullable=True)   # JSON list of tool names at planning time
    selection_justification = Column(Text, nullable=True)   # full planner reasoning
    expected_result         = Column(Text, nullable=True)   # planner predicted output
    constraint_validation   = Column(Text, nullable=True)   # JSON dict of constraints checked

    # Act / Observe
    tool_result      = Column(Text,        nullable=True)   # JSON-serialised tool output

    # Log / Respond
    final_response   = Column(Text,        nullable=True)
    error            = Column(Text,        nullable=True)   # JSON-serialised ToolError or null

    # Meta
    iterations_used  = Column(Integer,     nullable=True)
    halt_reason      = Column(String(64),  nullable=True)   # success | tool_failure | max_iterations | ambiguity

    # Decision trace — JSON array of {phase, data} steps (interpretation → selection → execution)
    trace            = Column(Text,        nullable=True)

    # Observability (Phase 2)
    latency_ms            = Column(Integer, nullable=True)  # wall-clock time for this run
    prompt_tokens         = Column(Integer, nullable=True)  # total LLM input tokens
    completion_tokens     = Column(Integer, nullable=True)  # total LLM output tokens
    cache_read_tokens     = Column(Integer, nullable=True)  # tokens served from prompt cache
    cache_creation_tokens = Column(Integer, nullable=True)  # tokens written to prompt cache


# ─── Table 2: sessions ────────────────────────────────────────────────────────
# Stores conversation history per session for memory.py.
# history is a JSON array of {role, content} turn objects.

class SessionRecord(Base):
    __tablename__ = "sessions"

    session_id   = Column(String(64),  primary_key=True)
    created_at   = Column(DateTime,    nullable=False, default=datetime.utcnow)
    last_active  = Column(DateTime,    nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    history      = Column(Text,        nullable=False, default="[]")  # JSON array of turns


# ─── Table 3: pending_confirmations ──────────────────────────────────────────
# Stores action confirmations suspended mid-run awaiting member approval.
# Policy: TTL 10 minutes. New inbound message cancels the pending entry.

class PendingConfirmation(Base):
    __tablename__ = "pending_confirmations"

    session_id  = Column(String(64),  primary_key=True)
    tool_name   = Column(String(128), nullable=False)
    payload     = Column(Text,        nullable=False)   # JSON-serialised ConfirmationPayload
    input_data  = Column(Text,        nullable=False)   # JSON-serialised tool input (confirmed=True replay)
    created_at  = Column(DateTime,    nullable=False, default=datetime.utcnow)
    expires_at  = Column(DateTime,    nullable=False)
    is_resolved = Column(Boolean,     nullable=False, default=False)


# ─── Table 4: demo_account_states ────────────────────────────────────────────
# Stores mutated demo account state so tool side-effects persist across requests.
# _get_account() in tools/customer_ops.py reads this table first and falls back
# to the hardcoded fixture if no row exists (first-run default).
# POST /accounts/{id}/reset deletes the row to restore fixture defaults.

class DemoAccountState(Base):
    __tablename__ = "demo_account_states"

    account_id = Column(String(64), primary_key=True, nullable=False)
    state      = Column(Text,       nullable=False)   # JSON-serialised AccountState fields
    updated_at = Column(DateTime,   nullable=False,   default=datetime.utcnow)


# ─── Table creation ───────────────────────────────────────────────────────────

def create_tables() -> None:
    """Create all tables if they do not exist. Called once at app startup."""
    Base.metadata.create_all(bind=engine)
    _migrate()


def _migrate() -> None:
    """Apply additive column migrations. Safe to re-run; errors on existing columns are silenced."""
    from sqlalchemy import text as _text
    _additive = [
        "ALTER TABLE agent_logs ADD COLUMN trace TEXT",
        "ALTER TABLE agent_logs ADD COLUMN latency_ms INTEGER",
        "ALTER TABLE agent_logs ADD COLUMN prompt_tokens INTEGER",
        "ALTER TABLE agent_logs ADD COLUMN completion_tokens INTEGER",
        "ALTER TABLE agent_logs ADD COLUMN cache_read_tokens INTEGER",
        "ALTER TABLE agent_logs ADD COLUMN cache_creation_tokens INTEGER",
    ]
    with engine.connect() as conn:
        for sql in _additive:
            try:
                conn.execute(_text(sql))
                conn.commit()
            except Exception:
                pass
