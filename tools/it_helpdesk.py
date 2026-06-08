"""
tools/it_helpdesk.py

IT Helpdesk tool implementations — stub domain demonstrating extensibility.

These tools mirror the structural patterns of tools/customer_ops.py:
  - Pydantic input models with validation
  - Structured ToolResult / ConfirmationPayload return types
  - Confirmation flow for destructive actions (reset_password)
  - Read-only tools return results directly (check_ticket_status)

Domain pack: domains/it_helpdesk.py
Activate:    DOMAIN_PACK=it_helpdesk in .env

Note: these are demo stubs. In production, replace the body of each
tool with real API calls to your ticketing system (ServiceNow, Jira, etc.).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from tools.registry import register_tool


# ─── Input models ─────────────────────────────────────────────────────────────

class CheckTicketInput(BaseModel):
    ticket_id: str = Field(description="Ticket identifier, e.g. 'INC-4521'.")
    account_id: str | None = Field(default=None, description="Employee account ID (injected by agent).")


class ResetPasswordInput(BaseModel):
    username: str = Field(description="Employee username or email.")
    account_id: str | None = Field(default=None, description="Employee account ID (injected by agent).")
    confirmed: bool = Field(default=False, description="True when the employee has confirmed the reset.")


class HardwareRequestInput(BaseModel):
    description: str = Field(description="Free-text description of the hardware issue or item needed.")
    account_id: str | None = Field(default=None, description="Employee account ID (injected by agent).")


class EscalateTicketInput(BaseModel):
    ticket_id: str = Field(description="Ticket to escalate, e.g. 'INC-4521'.")
    reason: str = Field(description="Brief justification for escalation.")
    account_id: str | None = Field(default=None, description="Employee account ID (injected by agent).")
    confirmed: bool = Field(default=False, description="True when the employee has confirmed escalation.")


# ─── Demo data ────────────────────────────────────────────────────────────────

_DEMO_TICKETS: dict[str, dict[str, Any]] = {
    "INC-1001": {
        "status":    "In Progress",
        "priority":  "Medium",
        "summary":   "VPN client fails to connect on Windows 11",
        "assignee":  "L2 Network Team",
        "created":   "2026-06-01",
        "updated":   "2026-06-07",
    },
    "INC-1002": {
        "status":    "Pending User",
        "priority":  "Low",
        "summary":   "Request for second monitor",
        "assignee":  "Hardware Team",
        "created":   "2026-06-03",
        "updated":   "2026-06-05",
    },
    "INC-1003": {
        "status":    "Resolved",
        "priority":  "High",
        "summary":   "Cannot access shared drive after password change",
        "assignee":  "Resolved — no further action",
        "created":   "2026-05-28",
        "updated":   "2026-06-04",
    },
}


# ─── Tools ────────────────────────────────────────────────────────────────────

@register_tool
def check_ticket_status(input: CheckTicketInput) -> dict[str, Any]:
    """Look up the current status and recent activity on a support ticket."""
    ticket = _DEMO_TICKETS.get(input.ticket_id.upper())
    if ticket is None:
        return {
            "error": {
                "code":    "TICKET_NOT_FOUND",
                "message": f"No ticket found with ID '{input.ticket_id}'.",
                "details": {"available_demo_ids": list(_DEMO_TICKETS)},
            }
        }
    return {
        "halt_reason": "success",
        "ticket_id":   input.ticket_id.upper(),
        **ticket,
        "response": (
            f"Ticket {input.ticket_id.upper()} — {ticket['status']}.\n"
            f"Summary: {ticket['summary']}\n"
            f"Priority: {ticket['priority']}  |  Assignee: {ticket['assignee']}\n"
            f"Last updated: {ticket['updated']}"
        ),
    }


@register_tool
def reset_password(input: ResetPasswordInput) -> dict[str, Any]:
    """Reset a user account password or unlock a locked account."""
    username = (input.username or "").strip()
    if not username:
        return {
            "error": {
                "code":    "USERNAME_REQUIRED",
                "message": "A username or email address is required to reset the password.",
                "details": {},
            }
        }

    if not input.confirmed:
        # First call: return a confirmation payload
        return {
            "halt_reason": "confirmation_pending",
            "confirmation_payload": {
                "action":          "Password Reset",
                "username":        username,
                "Billing impact":  "None",
                "Membership":      "Account will be temporarily locked during reset (< 5 minutes).",
                "Next step":       "A reset link will be emailed to the address on file.",
            },
            "tool_input_for_confirmation": {"username": username, "confirmed": True},
        }

    # Confirmed: execute the reset (demo stub)
    return {
        "halt_reason":      "success",
        "verified_changes": [f"Password reset initiated for '{username}'."],
        "response": (
            f"Password reset initiated for **{username}**. "
            "A reset link has been sent to the email address on file. "
            "The link expires in 24 hours."
        ),
    }


@register_tool
def hardware_request(input: HardwareRequestInput) -> dict[str, Any]:
    """Submit a hardware request or report a hardware issue."""
    if not (input.description or "").strip():
        return {
            "error": {
                "code":    "DESCRIPTION_REQUIRED",
                "message": "Please describe the hardware issue or item you need.",
                "details": {},
            }
        }

    # Demo stub: auto-assign a ticket number
    ticket_id = "INC-DEMO"
    return {
        "halt_reason":      "success",
        "verified_changes": [f"Hardware request submitted: {ticket_id}"],
        "response": (
            f"Hardware request submitted — ticket **{ticket_id}** created.\n"
            f"Description: {input.description}\n"
            "Expected response time: 1–2 business days."
        ),
    }


@register_tool
def escalate_ticket(input: EscalateTicketInput) -> dict[str, Any]:
    """Escalate an open support ticket to a higher-priority queue."""
    ticket_id = (input.ticket_id or "").strip().upper()
    if not ticket_id:
        return {
            "error": {
                "code":    "TICKET_ID_REQUIRED",
                "message": "A ticket ID is required to escalate.",
                "details": {},
            }
        }

    ticket = _DEMO_TICKETS.get(ticket_id)
    if ticket and ticket["status"] == "Resolved":
        return {
            "error": {
                "code":    "TICKET_ALREADY_RESOLVED",
                "message": f"Ticket {ticket_id} is already resolved and cannot be escalated.",
                "details": {},
            }
        }

    if not input.confirmed:
        return {
            "halt_reason": "confirmation_pending",
            "confirmation_payload": {
                "action":         "Ticket Escalation",
                "ticket_id":      ticket_id,
                "reason":         input.reason,
                "Billing impact": "None",
                "Membership":     "Ticket will be moved to priority queue.",
            },
            "tool_input_for_confirmation": {
                "ticket_id": ticket_id,
                "reason":    input.reason,
                "confirmed": True,
            },
        }

    return {
        "halt_reason":      "success",
        "verified_changes": [f"Ticket {ticket_id} escalated."],
        "response": (
            f"Ticket **{ticket_id}** has been escalated.\n"
            f"Reason: {input.reason}\n"
            "The team has been notified and will respond within 4 hours."
        ),
    }
