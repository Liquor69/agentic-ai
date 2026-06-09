"""
api/schemas.py

Pydantic request/response models for all API endpoints.
API contract: ARCHITECTURE.md — API Contract section.

No business logic here — shapes only.
"""

from __future__ import annotations

import json as _json
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


# ─── POST /run ────────────────────────────────────────────────────────────────

class RunRequest(BaseModel):
    message: str = Field(description="Natural language request from the member or operator.")
    session_id: str | None = Field(
        default=None,
        description="Existing session ID to continue. Omit to start a new session.",
    )
    allow_history_reference: bool = Field(
        default=True,
        description=(
            "When True, prior turns are injected into LLM context (stateful). "
            "When False, each request is fully stateless."
        ),
    )
    confirmed: bool | None = Field(
        default=None,
        description=(
            "Confirmation signal for pending actions. "
            "None = normal request. "
            "True = user pressed YES on a confirmation prompt. "
            "False = user pressed NO (cancel)."
        ),
    )
    form_data: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Form field values submitted when confirmed=True for a form-type pending action. "
            "Keys match the field names declared in the tool's form spec."
        ),
    )
    account_id: str | None = Field(
        default=None,
        description=(
            "Demo account ID selected in the Set Your Account panel. "
            "Injected into all tool calls — never ask the member for this."
        ),
    )
    dry_run: bool = Field(
        default=False,
        description=(
            "When True, the full agent loop runs (classify, plan, safety checks) "
            "but tool execution is simulated — no side effects. "
            "halt_reason will be 'dry_run'. "
            "Use for testing routing without modifying any account state."
        ),
    )


class TraceStep(BaseModel):
    """One phase of the perceive→plan→act→observe→log loop."""
    phase: str = Field(description="perceive | plan | act | observe | log")
    data: dict[str, Any] = Field(default_factory=dict)


class RunResponse(BaseModel):
    result: str = Field(description="Final response text for the member.")
    trace: list[TraceStep] = Field(description="Full decision trace, one entry per loop phase.")
    session_id: str
    iterations_used: int
    halt_reason: str | None = Field(
        default=None,
        description="success | tool_failure | max_iterations | ambiguity",
    )
    error: dict[str, Any] | None = None
    form_spec: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Present when halt_reason='form_required'. "
            "Describes the fields to render in the form panel."
        ),
    )
    # Observability (Phase 2)
    latency_ms: int = Field(
        default=0,
        description="Wall-clock time for this run in milliseconds.",
    )
    token_usage: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Accumulated LLM token counts for the run: "
            "input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens."
        ),
    )
    # Action safety (Phase 4)
    dry_run: bool = Field(
        default=False,
        description="True when the request was submitted with dry_run=True.",
    )


# ─── GET /logs ────────────────────────────────────────────────────────────────

class LogEntryOut(BaseModel):
    """Serialised view of an AgentLog row. No internal fields exposed."""
    id: int
    session_id: str
    timestamp: datetime
    input: str
    intent: str | None
    selected_tool: str | None
    reasoning: str | None
    available_tools: str | None          # JSON string — raw storage format
    selection_justification: str | None
    expected_result: str | None
    constraint_validation: str | None   # JSON string
    tool_result: str | None             # JSON string
    final_response: str | None
    error: str | None                   # JSON string
    iterations_used: int | None
    halt_reason: str | None
    trace: list[dict[str, Any]] | None = None
    # Observability (Phase 2)
    latency_ms: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None

    @field_validator("trace", mode="before")
    @classmethod
    def _decode_trace(cls, v: Any) -> list[dict[str, Any]] | None:
        if v is None:
            return None
        if isinstance(v, str):
            try:
                parsed = _json.loads(v)
                return parsed if isinstance(parsed, list) else None
            except Exception:
                return None
        return v

    model_config = {"from_attributes": True}


class LogsResponse(BaseModel):
    logs: list[LogEntryOut]
    total: int
    limit: int
    offset: int


# ─── GET /tools ───────────────────────────────────────────────────────────────

class ToolInfo(BaseModel):
    name: str
    description: str


# ─── GET /metrics ────────────────────────────────────────────────────────────

class LatencyStats(BaseModel):
    avg: float
    p50: int
    p95: int


class TokenStats(BaseModel):
    total_prompt: int
    total_completion: int
    total_cache_read: int
    total_cache_creation: int


class MetricsResponse(BaseModel):
    """Aggregated observability stats from agent_logs. Returned by GET /metrics."""
    period: dict[str, str | None]
    total_runs: int
    by_halt_reason: dict[str, int]
    latency_ms: LatencyStats
    tokens: TokenStats
    error_rate: float


# ─── GET /health ─────────────────────────────────────────────────────────────

class HealthResponse(BaseModel):
    status: str = Field(description="'ok' or 'degraded'")
    db: str     = Field(description="'ok' or error description")
    domain: str = Field(description="Active domain pack name.")
    tools_registered: int = Field(description="Number of tools currently registered.")
    version: str


# ─── POST /accounts/custom ────────────────────────────────────────────────────

class CustomAccountRequest(BaseModel):
    plan: str = Field(description="'standard' or 'premium'")
    status: str = Field(description="'active' | 'paused' | 'cancelled' | 'refunded' | 'expired'")
    billing_cycle: str = Field(description="'monthly' or 'annual'")
    last_payment_date: date = Field(description="Date of last payment (YYYY-MM-DD).")
    refund_history_present: bool
    pending_action: bool
