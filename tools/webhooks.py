"""
tools/webhooks.py

Outbound webhook dispatcher — sends member notifications via an n8n workflow.

This is the second integration pattern in the tool system, after pure Python logic
in customer_ops.py. Implementation differs; the planner's interface is identical:
one @register_tool function, same schema shape, same registry lookup.

To adapt to a different workflow:
  1. Change the webhook path  (e.g. /notify → /crm-update)
  2. Adjust the payload shape
  3. Zero changes to core/ required

DEMO STUB: set N8N_WEBHOOK_BASE_URL in .env to connect to a real n8n instance.
If unset, returns simulated success so the demo runs without external dependencies.
"""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field

from tools.customer_ops import ToolError
from tools.registry import register_tool


# ─── I/O models ───────────────────────────────────────────────────────────────

class NotificationInput(BaseModel):
    account_id: str = Field(description="Member account ID.")
    session_id: str = Field(description="Active session ID.")
    channel: str = Field(
        default="email",
        description="Delivery channel: 'email', 'sms', or 'push'.",
    )
    subject: str = Field(description="Notification subject or title.")
    message: str = Field(description="Notification body text.")


class NotificationOutput(BaseModel):
    success: bool
    channel: str
    simulated: bool = False   # True when N8N_WEBHOOK_BASE_URL is not configured
    webhook_status_code: int | None = None
    error: ToolError | None = None


# ─── Tool ─────────────────────────────────────────────────────────────────────

@register_tool
def send_member_notification(input: NotificationInput) -> NotificationOutput:
    """
    Send a notification to a member via the configured n8n notification workflow.
    Use for email, SMS, or push delivery of confirmations, alerts, or account updates.
    No confirmation required.
    """
    from config import settings

    base_url = settings.n8n_webhook_base_url.rstrip("/")

    if not base_url:
        # DEMO MODE — no webhook URL configured, simulate success.
        return NotificationOutput(success=True, channel=input.channel, simulated=True)

    import httpx

    payload = {
        "account_id": input.account_id,
        "session_id": input.session_id,
        "channel": input.channel,
        "subject": input.subject,
        "message": input.message,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    try:
        response = httpx.post(f"{base_url}/notify", json=payload, timeout=10.0)
        return NotificationOutput(
            success=response.is_success,
            channel=input.channel,
            webhook_status_code=response.status_code,
        )
    except httpx.TimeoutException:
        return NotificationOutput(
            success=False,
            channel=input.channel,
            error=ToolError(code="WEBHOOK_TIMEOUT", message="Notification webhook timed out."),
        )
    except Exception as exc:
        return NotificationOutput(
            success=False,
            channel=input.channel,
            error=ToolError(code="WEBHOOK_ERROR", message=str(exc)),
        )
