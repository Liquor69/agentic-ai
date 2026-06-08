"""
tools/external_api.py

Read-side external API integration — fetches member payment history
from a billing backend.

This is the third integration pattern in the tool system: authenticated HTTP reads
from an external service. The planner selects this when the user requests data that
lives outside the core account DB.

To adapt to a different data source:
  1. Replace the endpoint path and response mapping
  2. Update _MOCK_PAYMENT_HISTORY to match your domain
  3. Zero changes to core/ required

DEMO STUB: set EXTERNAL_API_BASE_URL and EXTERNAL_API_TOKEN in .env to connect
to a real billing backend. Without them, returns mock data so the demo runs.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from pydantic import BaseModel, Field

from tools.customer_ops import ToolError
from tools.registry import register_tool


# ─── Mock data (DEMO STUB) ────────────────────────────────────────────────────
# PRODUCTION replacement: GET {EXTERNAL_API_BASE_URL}/accounts/{account_id}/payments
# Expected response: {"transactions": [{"date", "amount", "description", "status"}, ...]}

_today = date.today()

_MOCK_PAYMENT_HISTORY: dict[str, list[dict[str, Any]]] = {
    "demo-pass-monthly": [
        {"date": (_today - timedelta(days=5)).isoformat(),   "amount":   60.0, "description": "Pass — Monthly",        "status": "paid"},
        {"date": (_today - timedelta(days=35)).isoformat(),  "amount":   60.0, "description": "Pass — Monthly",        "status": "paid"},
        {"date": (_today - timedelta(days=65)).isoformat(),  "amount":   60.0, "description": "Pass — Monthly",        "status": "paid"},
    ],
    "demo-pass-monthly-expired-window": [
        {"date": (_today - timedelta(days=20)).isoformat(),  "amount":   60.0, "description": "Pass — Monthly",        "status": "paid"},
        {"date": (_today - timedelta(days=50)).isoformat(),  "amount":   60.0, "description": "Pass — Monthly",        "status": "paid"},
    ],
    "demo-bc-annual": [
        {"date": (_today - timedelta(days=7)).isoformat(),   "amount": 1200.0, "description": "Black Card — Annual",   "status": "paid"},
        {"date": (_today - timedelta(days=372)).isoformat(), "amount": 1200.0, "description": "Black Card — Annual",   "status": "paid"},
    ],
    "demo-bc-cash": [
        {"date": (_today - timedelta(days=3)).isoformat(),   "amount":  120.0, "description": "Black Card — Monthly",  "status": "paid"},
        {"date": (_today - timedelta(days=3)).isoformat(),   "amount":   10.0, "description": "Replacement Card Fee",  "status": "paid"},
        {"date": (_today - timedelta(days=33)).isoformat(),  "amount":  120.0, "description": "Black Card — Monthly",  "status": "paid"},
    ],
    "demo-refund-used": [
        {"date": (_today - timedelta(days=2)).isoformat(),   "amount":   60.0, "description": "Pass — Monthly",        "status": "paid"},
        {"date": (_today - timedelta(days=32)).isoformat(),  "amount":  -60.0, "description": "Refund — Pass Monthly", "status": "refunded"},
        {"date": (_today - timedelta(days=32)).isoformat(),  "amount":   60.0, "description": "Pass — Monthly",        "status": "paid"},
    ],
    "demo-pending": [
        {"date": (_today - timedelta(days=1)).isoformat(),   "amount":   60.0, "description": "Pass — Monthly",        "status": "paid"},
        {"date": (_today - timedelta(days=31)).isoformat(),  "amount":   60.0, "description": "Pass — Monthly",        "status": "paid"},
    ],
    "demo-paused": [
        {"date": (_today - timedelta(days=20)).isoformat(),  "amount":  120.0, "description": "Black Card — Monthly",  "status": "paid"},
        {"date": (_today - timedelta(days=20)).isoformat(),  "amount":   20.0, "description": "Pause Fee (2 months)",  "status": "paid"},
        {"date": (_today - timedelta(days=50)).isoformat(),  "amount":  120.0, "description": "Black Card — Monthly",  "status": "paid"},
    ],
}


# ─── I/O models ───────────────────────────────────────────────────────────────

class PaymentHistoryInput(BaseModel):
    account_id: str = Field(description="Member account ID.")
    session_id: str = Field(description="Active session ID.")
    limit: int = Field(
        default=10,
        description="Maximum number of transactions to return (1–24).",
    )


class Transaction(BaseModel):
    date: str
    amount: float           # negative = refund
    description: str
    status: str             # "paid" | "refunded" | "pending"


class PaymentHistoryOutput(BaseModel):
    account_id: str
    transactions: list[Transaction]
    total_returned: int
    simulated: bool = False
    error: ToolError | None = None


# ─── Tool ─────────────────────────────────────────────────────────────────────

@register_tool
def fetch_payment_history(input: PaymentHistoryInput) -> PaymentHistoryOutput:
    """
    Fetch a member's recent payment history from the billing backend.
    Use when the member asks about past charges, billing dates, or transaction records.
    Read-only — no confirmation required.
    """
    from config import settings

    limit = max(1, min(input.limit, 24))
    base_url = getattr(settings, "external_api_base_url", "").rstrip("/")
    token = settings.external_api_token

    if not base_url:
        # DEMO MODE — no API URL configured, return mock data.
        rows = _MOCK_PAYMENT_HISTORY.get(input.account_id)
        if rows is None:
            return PaymentHistoryOutput(
                account_id=input.account_id,
                transactions=[],
                total_returned=0,
                simulated=True,
                error=ToolError(
                    code="ACCOUNT_NOT_FOUND",
                    message=f"No payment history found for account '{input.account_id}'.",
                ),
            )
        sliced = rows[:limit]
        return PaymentHistoryOutput(
            account_id=input.account_id,
            transactions=[Transaction(**t) for t in sliced],
            total_returned=len(sliced),
            simulated=True,
        )

    # PRODUCTION — real API call
    import httpx

    try:
        response = httpx.get(
            f"{base_url}/accounts/{input.account_id}/payments",
            headers={"Authorization": f"Bearer {token}"},
            params={"limit": limit},
            timeout=10.0,
        )
        if not response.is_success:
            return PaymentHistoryOutput(
                account_id=input.account_id,
                transactions=[],
                total_returned=0,
                error=ToolError(
                    code="API_ERROR",
                    message=f"Billing API returned HTTP {response.status_code}.",
                ),
            )
        data = response.json()
        txns = [Transaction(**t) for t in data.get("transactions", [])]
        return PaymentHistoryOutput(
            account_id=input.account_id,
            transactions=txns,
            total_returned=len(txns),
        )
    except httpx.TimeoutException:
        return PaymentHistoryOutput(
            account_id=input.account_id,
            transactions=[],
            total_returned=0,
            error=ToolError(code="API_TIMEOUT", message="Billing API timed out."),
        )
    except Exception as exc:
        return PaymentHistoryOutput(
            account_id=input.account_id,
            transactions=[],
            total_returned=0,
            error=ToolError(code="API_ERROR", message=str(exc)),
        )
