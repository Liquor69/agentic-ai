"""
api/routes.py

POST /run   — run the agent on a natural language request
GET  /logs  — query structured run logs
GET  /tools — list registered tools (names + descriptions only)

The /run endpoint calls core.agent.run_agent() — the single integration point
between the API layer and the agent core. When core/agent.py is implemented,
this file requires zero changes.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from api.schemas import CustomAccountRequest, HealthResponse, LogEntryOut, LogsResponse, MetricsResponse, RunRequest, RunResponse, ToolInfo
from config import settings
from db import crud
from tools.registry import get_tool_names_and_descriptions

router = APIRouter()


# ─── Auth dependency ──────────────────────────────────────────────────────────

async def verify_api_key(x_api_key: Annotated[str | None, Header()] = None) -> None:
    """
    Static API key check. Reads API_KEY from .env.
    If API_KEY is empty, auth is disabled (development mode).
    """
    if settings.api_key and x_api_key != settings.api_key:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key header.")


# ─── POST /run ────────────────────────────────────────────────────────────────

@router.post("/run", response_model=RunResponse, dependencies=[Depends(verify_api_key)])
async def run(request: RunRequest) -> RunResponse:
    """
    Submit a natural language request to the agent.

    Set confirmed=true to confirm a pending action (YES button).
    Set confirmed=false to cancel a pending action (NO button).
    Omit confirmed (or null) for all normal requests.
    """
    import logging
    from core.agent import run_agent

    try:
        return await asyncio.to_thread(
            run_agent,
            message=request.message,
            session_id=request.session_id,
            allow_history_reference=request.allow_history_reference,
            confirmed=request.confirmed,
            account_id=request.account_id,
            form_data=request.form_data,
            dry_run=request.dry_run,
        )
    except Exception as exc:
        logging.getLogger(__name__).exception("Unhandled exception in /run route")
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}")


# ─── GET /logs ────────────────────────────────────────────────────────────────

@router.get("/logs", response_model=LogsResponse, dependencies=[Depends(verify_api_key)])
async def get_logs(
    session_id: str | None       = Query(None, description="Filter by session ID."),
    intent: str | None           = Query(None, description="Filter by classified intent."),
    selected_tool: str | None    = Query(None, description="Filter by tool name."),
    has_error: bool | None       = Query(None, description="True: only errored runs. False: only clean runs."),
    since: datetime | None       = Query(None, description="Earliest timestamp (ISO 8601)."),
    until: datetime | None       = Query(None, description="Latest timestamp (ISO 8601)."),
    limit: int                   = Query(100, ge=1, le=500, description="Max rows to return."),
    offset: int                  = Query(0, ge=0, description="Pagination offset."),
) -> LogsResponse:
    """Query agent run logs with optional filters."""
    logs = crud.get_logs(
        session_id=session_id,
        intent=intent,
        selected_tool=selected_tool,
        has_error=has_error,
        since=since,
        until=until,
        limit=limit,
        offset=offset,
    )
    return LogsResponse(
        logs=[LogEntryOut.model_validate(log) for log in logs],
        total=len(logs),
        limit=limit,
        offset=offset,
    )


# ─── GET /tools ───────────────────────────────────────────────────────────────

@router.get("/tools", response_model=list[ToolInfo])
async def get_tools() -> list[ToolInfo]:
    """List all registered tools. Returns names and descriptions only — no schemas."""
    return [ToolInfo(**t) for t in get_tool_names_and_descriptions()]


# ─── GET /metrics ────────────────────────────────────────────────────────────

@router.get("/metrics", response_model=MetricsResponse, dependencies=[Depends(verify_api_key)])
async def get_metrics(
    since: datetime | None = Query(None, description="Earliest timestamp (ISO 8601)."),
    until: datetime | None = Query(None, description="Latest timestamp (ISO 8601)."),
) -> MetricsResponse:
    """
    Aggregated observability stats from agent_logs.

    Returns total runs, halt-reason breakdown, latency percentiles (p50/p95),
    cumulative token counts, and error rate. Optionally filtered to a time window.
    """
    data = crud.get_metrics(since=since, until=until)
    return MetricsResponse(**data)


# ─── GET /dashboard ───────────────────────────────────────────────────────────

@router.get("/dashboard", include_in_schema=False)
async def dashboard() -> dict:
    """
    Lightweight metrics dashboard data.
    Returns the same payload as /metrics plus a last-10-runs preview.
    Consumed by frontend/index.html to populate the Dashboard tab.
    """
    metrics = crud.get_metrics()
    recent_logs = crud.get_logs(limit=10)
    recent = [
        {
            "id":          log.id,
            "session_id":  log.session_id,
            "timestamp":   log.timestamp.isoformat() if log.timestamp else None,
            "intent":      log.intent,
            "halt_reason": log.halt_reason,
            "latency_ms":  log.latency_ms,
            "error":       bool(log.error),
        }
        for log in recent_logs
    ]
    return {**metrics, "recent_runs": recent}


# ─── GET /health ─────────────────────────────────────────────────────────────

@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """
    Liveness + readiness probe.

    Returns 200 when the API is running and the database is reachable.
    Returns 200 with status='degraded' when the DB is unavailable (so load
    balancers can distinguish liveness from readiness if needed).
    """
    from tools.registry import list_tools

    db_status = "ok"
    try:
        from db import crud
        crud.get_logs(limit=1)
    except Exception as exc:
        db_status = f"error: {exc}"

    return HealthResponse(
        status="ok" if db_status == "ok" else "degraded",
        db=db_status,
        domain=settings.domain_pack,
        tools_registered=len(list_tools()),
        version="1.0.0",
    )


# ─── GET /accounts ────────────────────────────────────────────────────────────

@router.get("/accounts")
async def get_accounts() -> list[dict]:
    """List demo account archetypes for the Set Your Account panel."""
    from tools.customer_ops import ARCHETYPES
    return ARCHETYPES


# ─── POST /accounts/custom ────────────────────────────────────────────────────

@router.post("/accounts/custom")
async def set_custom_account(body: CustomAccountRequest) -> dict:
    """
    Register (or overwrite) the custom demo account.
    Returns {"account_id": "custom"} on success.
    """
    from tools.customer_ops import set_custom_account as _set
    resolved_status = _set(body.model_dump())
    return {"account_id": "custom", "resolved_status": resolved_status}
