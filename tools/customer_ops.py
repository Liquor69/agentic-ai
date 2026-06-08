"""
tools/customer_ops.py

Default tool configuration: Fitness World customer operations.

This is ONE instantiation of the general tool architecture — not the system's identity.
To adapt for a different domain: create a new file in tools/, decorate with @register_tool,
and the planner picks it up automatically via the registry. Zero core changes required.

Policy authority: CUSTOMER_OPS_POLICY.md
FAQ authority:    FAQ.md

Architecture rule enforced here:
  All policy constraints live in this file — not in the planner.
  The planner selects tools. Tools enforce their own eligibility rules.
"""

from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, model_validator

from tools.registry import register_tool

# ─── Load FAQ and policy from source-of-truth files ──────────────────────────
# FAQ.md and CUSTOMER_OPS_POLICY.md are the single source of truth.
# Loaded once at import so LLM tools can cache them as stable prefixes.

_DOCS_DIR = Path(__file__).parent.parent  # project root

FAQ_CONTENT = (_DOCS_DIR / "FAQ.md").read_text(encoding="utf-8")
POLICY_SUPPLEMENT = (_DOCS_DIR / "CUSTOMER_OPS_POLICY.md").read_text(encoding="utf-8")
PRODUCT_KNOWLEDGE = FAQ_CONTENT + "\n\n" + POLICY_SUPPLEMENT



# ─── Shared primitive types ───────────────────────────────────────────────────

class ToolError(BaseModel):
    """Structured error returned at the tool execution layer. Never raises exceptions to the agent."""
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ConfirmationPayload(BaseModel):
    """
    Returned by action tools (4–7) when confirmed=False.
    The agent suspends, persists this to DB, and awaits member confirmation.
    """
    action: str
    summary: str
    billing_impact: str
    member_impact: str
    guest_pass_impact: str
    subscription_validity_impact: str
    raw: dict[str, Any] = Field(default_factory=dict)


# ─── Account state model ──────────────────────────────────────────────────────

class AccountState(BaseModel):
    """
    The 7 fields stored in the member DB. Everything else is derived.
    Returned by _get_account(). In production: fetched from your backend API.

    status: "active" | "paused" | "cancelled" | "refunded" | "expired"
      active    — subscription running, auto-renewal on
      paused    — subscription paused
      cancelled — auto-renewal off; subscription still valid until period end
      refunded  — subscription invalidated after refund
      expired   — period ended with no renewal
    """
    account_id: str
    plan: str                  # "pass" | "black_card"
    status: str
    billing_cycle: str         # "monthly" | "annual"
    last_payment_date: date
    refund_history_present: bool
    pending_action: bool

    # ── Derived (not persisted) ───────────────────────────────────────────────

    @property
    def is_active(self) -> bool:
        """Subscription still has a valid period."""
        return self.status in ("active", "paused", "cancelled")

    @property
    def is_paused(self) -> bool:
        return self.status == "paused"

    @property
    def auto_renewal(self) -> bool:
        """False when status is 'cancelled' (auto-renew disabled, still valid until period end)."""
        return self.status != "cancelled"

    @property
    def subscription_valid_until(self) -> date:
        days = 365 if self.billing_cycle == "annual" else 30
        return self.last_payment_date + timedelta(days=days)

    @property
    def plan_price(self) -> float:
        """
        Refundable plan price only.
        Extra fees (card replacement, pause fees, etc.) are non-refundable.
        """
        return _PLAN_PRICES.get(self.plan, {}).get(self.billing_cycle, 0.0)


# ─── Mock account layer ───────────────────────────────────────────────────────
# DEMO LAYER — replace _get_account() with a real API call in production.
# Exactly one function crosses the boundary between policy logic and external data.

# ─── Demo archetypes (public) ─────────────────────────────────────────────────
# Each entry mirrors a key in _MOCK_ACCOUNTS and adds display metadata for the UI.

ARCHETYPES: list[dict[str, Any]] = [
    {
        "id": "demo-pass-monthly",
        "label": "Pass · Monthly",
        "tags": ["Refund eligible"],
        "description": "Active monthly Pass member, purchased 5 days ago. Within the 14-day refund window.",
    },
    {
        "id": "demo-pass-monthly-expired-window",
        "label": "Pass · Monthly · Window expired",
        "tags": ["No refund"],
        "description": "Monthly Pass member purchased 20 days ago. Refund window has closed.",
    },
    {
        "id": "demo-bc-annual",
        "label": "Black Card · Annual",
        "tags": ["Refund eligible", "High value"],
        "description": "Annual Black Card member, purchased 7 days ago. Full €1,200 refundable. Guest pass included.",
    },
    {
        "id": "demo-bc-cash",
        "label": "Black Card · Monthly",
        "tags": ["Refund eligible"],
        "description": "Active monthly Black Card member, purchased 3 days ago. Within the 14-day refund window. Guest pass included.",
    },
    {
        "id": "demo-refund-used",
        "label": "Pass · Monthly · Refund used",
        "tags": ["Refund escalates"],
        "description": "Monthly Pass member with a prior refund on record. Further refund requests are escalated to support.",
    },
    {
        "id": "demo-pending",
        "label": "Pass · Monthly · Pending op",
        "tags": ["Blocked", "Has pending operation"],
        "description": "Monthly Pass member with an active pending operation. All new action requests are blocked until resolved.",
    },
    {
        "id": "demo-paused",
        "label": "Black Card · Monthly · Paused",
        "tags": ["Currently paused", "Plan change blocked"],
        "description": "Monthly Black Card member on a pause. Plan changes blocked; refund window closed.",
    },
]


def describe_account(account_id: str) -> str:
    """Return a concise LLM-readable account summary for planner context injection."""
    acct = _get_account(account_id)
    if isinstance(acct, ToolError):
        return f"account_id={account_id} (not found)"
    return (
        f"account_id={acct.account_id} | "
        f"plan={acct.plan}/{acct.billing_cycle} | "
        f"status={acct.status} | "
        f"refund_history_present={acct.refund_history_present} | "
        f"pending_action={acct.pending_action} | "
        f"last_payment_date={acct.last_payment_date.isoformat()} | "
        f"valid_until={acct.subscription_valid_until.isoformat()} | "
        f"plan_price=€{acct.plan_price:.2f}"
    )


def get_pause_preview_context(account_id: str) -> dict | None:
    """
    Return structured account data for the pause form's client-side live preview.
    Called from form_node when building the pause form spec.
    Returns None if the account is not found.
    """
    acct = _get_account(account_id)
    if isinstance(acct, ToolError) or acct is None:
        return None
    return {
        "subscription_valid_until": acct.subscription_valid_until.isoformat(),
        "has_guest_pass": acct.plan == "black_card",
        "fee_per_period": PAUSE_FEE_PER_MONTH,
        "fee_max_days": PAUSE_FEE_MAX_DAYS,
        "escalation_threshold_days": PAUSE_ESCALATE_DAYS,
        "min_days": PAUSE_MIN_DAYS,
    }


_MOCK_ACCOUNTS: dict[str, dict[str, Any]] = {
    "demo-pass-monthly": {
        "account_id": "demo-pass-monthly",
        "plan": "pass",
        "status": "active",
        "billing_cycle": "monthly",
        "last_payment_date": date.today() - timedelta(days=5),
        "refund_history_present": False,
        "pending_action": False,
    },
    "demo-pass-monthly-expired-window": {
        "account_id": "demo-pass-monthly-expired-window",
        "plan": "pass",
        "status": "active",
        "billing_cycle": "monthly",
        "last_payment_date": date.today() - timedelta(days=20),
        "refund_history_present": False,
        "pending_action": False,
    },
    "demo-bc-annual": {
        "account_id": "demo-bc-annual",
        "plan": "black_card",
        "status": "active",
        "billing_cycle": "annual",
        "last_payment_date": date.today() - timedelta(days=7),
        "refund_history_present": False,
        "pending_action": False,
    },
    "demo-bc-cash": {
        # Payment method is not stored in the DB — refund goes via original payment method.
        # A €10 replacement card fee was charged separately; only the plan price (€120) is refundable.
        "account_id": "demo-bc-cash",
        "plan": "black_card",
        "status": "active",
        "billing_cycle": "monthly",
        "last_payment_date": date.today() - timedelta(days=3),
        "refund_history_present": False,
        "pending_action": False,
    },
    "demo-refund-used": {
        "account_id": "demo-refund-used",
        "plan": "pass",
        "status": "active",
        "billing_cycle": "monthly",
        "last_payment_date": date.today() - timedelta(days=2),
        "refund_history_present": True,
        "pending_action": False,
    },
    "demo-pending": {
        "account_id": "demo-pending",
        "plan": "pass",
        "status": "active",
        "billing_cycle": "monthly",
        "last_payment_date": date.today() - timedelta(days=1),
        "refund_history_present": False,
        "pending_action": True,
    },
    "demo-paused": {
        # last_payment_date 20 days ago → refund window expired (>14 days).
        # subscription_valid_until derived as last_payment_date + 30 = today + 10 days.
        "account_id": "demo-paused",
        "plan": "black_card",
        "status": "paused",
        "billing_cycle": "monthly",
        "last_payment_date": date.today() - timedelta(days=20),
        "refund_history_present": False,
        "pending_action": False,
    },
}


def _get_account(account_id: str) -> AccountState | ToolError:
    """
    DEMO STUB — returns a mock account.
    Production replacement: GET /accounts/{account_id} from your member management backend.
    Returns ToolError if account is not found.
    """
    data = _MOCK_ACCOUNTS.get(account_id)
    if data is None:
        return ToolError(
            code="ACCOUNT_NOT_FOUND",
            message=f"No account found with ID '{account_id}'.",
            details={"account_id": account_id},
        )
    return AccountState(**data)


def set_custom_account(data: dict) -> str:
    """
    Register (or overwrite) the 'custom' demo account with caller-supplied parameters.
    Called by POST /accounts/custom. account_id is always 'custom'.

    'expired' is a derived state — it cannot be set directly.
    If status='active' and last_payment_date + billing_cycle_days < today,
    status is automatically resolved to 'expired'.

    Returns the resolved status string.
    """
    last_payment = data.get("last_payment_date")
    if isinstance(last_payment, str):
        last_payment = date.fromisoformat(last_payment)

    billing_cycle = data["billing_cycle"]
    billing_days  = 365 if billing_cycle == "annual" else 30
    valid_until   = last_payment + timedelta(days=billing_days)

    status = data["status"]
    if status == "active" and valid_until < date.today():
        status = "expired"

    _MOCK_ACCOUNTS["custom"] = {
        "account_id": "custom",
        "plan": data["plan"],
        "status": status,
        "billing_cycle": billing_cycle,
        "last_payment_date": last_payment,
        "refund_history_present": bool(data["refund_history_present"]),
        "pending_action": bool(data["pending_action"]),
    }
    return status


# ─── Plan price table ─────────────────────────────────────────────────────────

_PLAN_PRICES: dict[str, dict[str, float]] = {
    "pass":       {"monthly": 60.0,   "annual": 600.0},
    "black_card": {"monthly": 120.0,  "annual": 1_200.0},
}

REFUND_WINDOW_DAYS    = 14   # calendar days from last_payment_date
# Refund covers plan price only — extra fees (card replacement, pause fees) are non-refundable.
# One refund per account lifetime (enforced via refund_history_present: bool in DB).
PAUSE_MIN_NOTICE_DAYS = 14   # pause request must arrive ≥ this many days before start
PAUSE_FEE_PER_MONTH  = 10.0  # charged per 30-day period for paid pauses
PAUSE_MIN_DAYS       = 30    # minimum pause (1 month)
PAUSE_FEE_MAX_DAYS   = 90    # 30–90 days (1–3 months): fee tier
PAUSE_ESCALATE_DAYS  = 91    # 91–180 days (4–6 months): escalation without docs
PAUSE_DENY_DAYS      = 181   # 181+ days (7+ months): denied
REPLACEMENT_CARD_FEE = 10.0
CONFIRMATION_TTL_SEC = 600   # 10 minutes


# ─── Policy check helpers ─────────────────────────────────────────────────────
# Each returns None on pass, ToolError on failure.
# Called by tool functions — never by the planner.

def _block_if_pending(acct: AccountState) -> ToolError | None:
    if acct.pending_action:
        return ToolError(
            code="PENDING_OPERATION_ACTIVE",
            message=(
                "A pending operation is active on this account. "
                "Please resolve or cancel it before submitting a new request."
            ),
        )
    return None


def _block_if_paused(acct: AccountState) -> ToolError | None:
    if acct.is_paused:
        return ToolError(
            code="ACCOUNT_PAUSED",
            message="This action is not available while the account is paused.",
        )
    return None


def _require_active_subscription(acct: AccountState) -> ToolError | None:
    if not acct.is_active:
        return ToolError(
            code="NO_ACTIVE_SUBSCRIPTION",
            message="This account does not have an active subscription.",
        )
    return None


def _check_refund_window(acct: AccountState) -> ToolError | None:
    days_elapsed = (date.today() - acct.last_payment_date).days
    if days_elapsed > REFUND_WINDOW_DAYS:
        return ToolError(
            code="REFUND_WINDOW_EXPIRED",
            message=(
                f"The refund window has expired. "
                f"Purchase was {days_elapsed} days ago (limit: {REFUND_WINDOW_DAYS} days)."
            ),
            details={"days_elapsed": days_elapsed, "window_days": REFUND_WINDOW_DAYS},
        )
    return None


def _check_refund_lifetime(acct: AccountState) -> ToolError | None:
    if acct.refund_history_present:
        return ToolError(
            code="REFUND_ESCALATION_REQUIRED",
            message=(
                "A previous refund is on record for this account. "
                "Further refund requests require manual review by the support team. "
                "Please contact support@fitness.com."
            ),
        )
    return None


def _check_pause_notice(start_date: date) -> ToolError | None:
    notice_days = (start_date - date.today()).days
    if notice_days < PAUSE_MIN_NOTICE_DAYS:
        return ToolError(
            code="PAUSE_NOTICE_INSUFFICIENT",
            message=(
                f"Pause requests must be submitted at least {PAUSE_MIN_NOTICE_DAYS} days "
                f"in advance of the start date. Current notice: {notice_days} day(s)."
            ),
            details={"notice_days": notice_days, "required_days": f">={PAUSE_MIN_NOTICE_DAYS}"},
        )
    return None


def _check_pause_duration(duration_days: int, has_documentation: bool) -> ToolError | None:
    if duration_days < PAUSE_MIN_DAYS:
        return ToolError(
            code="PAUSE_DURATION_TOO_SHORT",
            message=f"The minimum pause duration is 30 days (1 month). Requested: {duration_days} days.",
            details={"requested_days": duration_days, "minimum_days": PAUSE_MIN_DAYS},
        )
    if duration_days >= PAUSE_DENY_DAYS:
        return ToolError(
            code="PAUSE_DURATION_DENIED",
            message="Pauses longer than 180 days (6 months) are not available.",
            details={"requested_days": duration_days, "maximum_days": 180},
        )
    if has_documentation:
        # Staff must validate documents before the pause can be applied
        return ToolError(
            code="PAUSE_DOCUMENTATION_ESCALATION",
            message=(
                "Your pause request has been received. "
                "Once your documents are validated, your subscription will be paused "
                f"for the selected period ({duration_days} days). "
                "Please submit your documentation to support@fitness.com."
            ),
            details={"requested_days": duration_days},
        )
    if duration_days >= PAUSE_ESCALATE_DAYS:
        return ToolError(
            code="PAUSE_ESCALATION_REQUIRED",
            message=(
                f"A {duration_days}-day pause requires escalation to support. "
                "Please contact support@fitness.com."
            ),
            details={"requested_days": duration_days},
        )
    return None


# ─── Billing calculation helpers ──────────────────────────────────────────────

def _pause_fee(duration_days: int) -> float:
    """Returns the pause fee in euros. Fee is per 30-day period, rounded up."""
    periods = math.ceil(duration_days / 30)
    return periods * PAUSE_FEE_PER_MONTH


def _upgrade_delta_charge(acct: AccountState, new_billing_cycle: str) -> float:
    """
    Delta billing formula for upgrades:
      charge = new_plan_price − (days_remaining × current_daily_rate)
    Floored at 0. Rounded to 2 decimal places.
    """
    days_remaining = max(0, (acct.subscription_valid_until - date.today()).days)
    days_in_period = 30 if acct.billing_cycle == "monthly" else 365
    current_daily_rate = _PLAN_PRICES[acct.plan][acct.billing_cycle] / days_in_period
    new_price = _PLAN_PRICES["black_card"][new_billing_cycle]
    charge = new_price - (days_remaining * current_daily_rate)
    return round(max(0.0, charge), 2)


# ─────────────────────────────────────────────────────────────────────────────
# TOOL 1: FAQ
# ─────────────────────────────────────────────────────────────────────────────

class FAQLookupInput(BaseModel):
    question: str = Field(description="The member's verbatim question.")
    session_id: str = Field(description="Active session ID.")


class FAQLookupOutput(BaseModel):
    answer: str
    found_in_faq: bool
    source: str = "Fitness World FAQ"
    error: ToolError | None = None


@register_tool
def faq_lookup(input: FAQLookupInput) -> FAQLookupOutput:
    """
    Answer a member's question using the Fitness World FAQ document.
    Route here when the question topic is covered by the FAQ.
    No API call. No confirmation required.
    """
    from config import llm_call  # config is the sole provider SDK surface

    messages = [
        {
            "role": "user",
            "content": (
                "Answer the following member question using ONLY the FAQ content provided. "
                "If the answer is not in the FAQ, say exactly: 'This question is not covered by the FAQ.' "
                "Be factual and specific — use exact figures (prices, durations, counts). "
                "For broad or general questions, cover all relevant sections from the FAQ completely.\n\n"
                f"FAQ:\n{FAQ_CONTENT}\n\n"
                f"Question: {input.question}"
            ),
        }
    ]
    response = llm_call(
        messages=messages,
        system="You are a Fitness World customer support assistant. Answer only from the provided FAQ.",
        tools=None,
        model_tier="fast",
    )

    if isinstance(response, dict) and "error" in response:
        return FAQLookupOutput(
            answer="",
            found_in_faq=False,
            error=ToolError(code="LLM_CALL_FAILED", message=response["error"]),
        )

    answer_text: str = response.get("content", "")
    found = "not covered by the faq" not in answer_text.lower()
    return FAQLookupOutput(answer=answer_text, found_in_faq=found)


# ─────────────────────────────────────────────────────────────────────────────
# TOOL 2: Policy Non-FAQ
# ─────────────────────────────────────────────────────────────────────────────

class PolicyQueryInput(BaseModel):
    question: str = Field(description="The member's policy question (not covered by FAQ).")
    session_id: str = Field(description="Active session ID.")


class PolicyQueryOutput(BaseModel):
    answer: str
    reclassify_to: str | None = Field(
        default=None,
        description=(
            "If the question implies the member wants to take an action, "
            "this is the name of the correct action tool to route to next."
        ),
    )
    source: str = "Fitness World Policy"
    error: ToolError | None = None


_RECLASSIFY_SYSTEM = """\
You are a Fitness World support assistant with access to the complete product knowledge base.
Answer the member's question directly and factually.

Rules:
1. Always answer the question. Never deflect, refer to other documents, suggest contacting support, \
or claim the question is outside your scope — the knowledge base contains the answer.
2. Use exact figures from the knowledge base (prices, durations, locations, hours). Do not paraphrase \
or omit specifics.
3. For broad or general questions, cover all relevant sections comprehensively — do not stop after \
the first matching section.
4. Do not invent anything not present in the knowledge base.
5. Set "reclassify_to" ONLY when the question implies the member wants to execute an account action \
(refund, cancel, pause, plan change). Never set it for informational questions — answer those inline.

Valid reclassify_to values: process_refund, cancel_subscription, pause_subscription, change_plan.
(Do NOT set reclassify_to: "faq_lookup" — answer the question yourself instead.)

Always respond as valid JSON: {"answer": "...", "reclassify_to": "<tool_name_or_null>"}\
"""


@register_tool
def policy_query(input: PolicyQueryInput) -> PolicyQueryOutput:
    """
    Answer a policy question not covered by the FAQ document.
    Informational only — no API call, no confirmation required.
    If the question implies an account action, returns reclassify_to with the target tool name.
    """
    from config import llm_call

    messages = [
        {
            "role": "user",
            "content": (
                f"Product knowledge:\n{PRODUCT_KNOWLEDGE}\n\n"
                f"Member question: {input.question}"
            ),
        }
    ]
    response = llm_call(
        messages=messages,
        system=_RECLASSIFY_SYSTEM,
        tools=None,
        model_tier="fast",
    )

    if isinstance(response, dict) and "error" in response:
        return PolicyQueryOutput(
            answer="",
            error=ToolError(code="LLM_CALL_FAILED", message=response["error"]),
        )

    raw: str = response.get("content", "")
    try:
        parsed = json.loads(raw)
        return PolicyQueryOutput(
            answer=parsed.get("answer", raw),
            reclassify_to=parsed.get("reclassify_to"),
        )
    except json.JSONDecodeError:
        # LLM returned prose instead of JSON — surface it as-is, no reclassification
        return PolicyQueryOutput(answer=raw)


# ─────────────────────────────────────────────────────────────────────────────
# TOOL 3: Bug / Problem  [EXCLUDED FROM DEMO SCOPE]
# ─────────────────────────────────────────────────────────────────────────────

class BugReportInput(BaseModel):
    description: str = Field(description="Member's description of the bug or problem.")
    session_id: str = Field(description="Active session ID.")


class BugReportOutput(BaseModel):
    message: str
    escalated: bool = True


@register_tool
def bug_report(input: BugReportInput) -> BugReportOutput:
    """
    [STUB — excluded from demo scope]
    Escalates bug and technical problem reports to the support team.
    """
    return BugReportOutput(
        message=(
            "Technical issues and bug reports are handled by our support team. "
            "Please contact support@fitness.com with a description of the problem."
        ),
        escalated=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# TOOL 4: Refund
# ─────────────────────────────────────────────────────────────────────────────

class RefundInput(BaseModel):
    account_id: str = Field(description="Member account ID.")
    session_id: str = Field(description="Active session ID.")
    confirmed: bool = Field(
        default=False,
        description=(
            "Set False on first call to receive a confirmation payload. "
            "Set True only after the member has explicitly confirmed."
        ),
    )


class RefundOutput(BaseModel):
    success: bool
    refund_amount: float | None = None
    currency: str | None = None              # ISO 4217 currency code, e.g. "EUR"
    refund_method: str | None = None
    processing_timeline: str | None = None   # e.g. "3–10 business days"
    subscription_status: str | None = None
    guest_pass_status: str | None = None
    confirmation_required: bool = False
    confirmation_payload: ConfirmationPayload | None = None
    escalation_required: bool = False
    error: ToolError | None = None


@register_tool
def process_refund(input: RefundInput) -> RefundOutput:
    """
    Process a full refund for a member account.

    Eligibility (all must pass):
      - No pending operation on account
      - Purchase ≤ 14 days ago
      - Account has not previously received a refund (1 per lifetime)

    A cancelled subscription (autoRenewal=false, still in prepaid period) is still eligible.
    Refund amount: subscription plan price only — extra fees (pause fee, replacement card) are non-refundable.
    Effects: subscription invalidated immediately; guest pass invalidated (Black Card only).
    Refund method: billing card (electronic) or cash (cash payment).

    First call (confirmed=False): returns confirmation_payload for member review.
    Second call (confirmed=True): executes the refund.
    """
    acct = _get_account(input.account_id)
    if isinstance(acct, ToolError):
        return RefundOutput(success=False, error=acct)

    # Policy checks — order matches CUSTOMER_OPS_POLICY.md
    if err := _block_if_pending(acct):
        return RefundOutput(success=False, error=err)

    # A cancelled-but-still-active subscription is refund eligible; only fully-expired/refunded is not.
    if not acct.is_active:
        if (date.today() - acct.last_payment_date).days > REFUND_WINDOW_DAYS:
            return RefundOutput(
                success=False,
                error=ToolError(
                    code="NO_ACTIVE_SUBSCRIPTION",
                    message="This account does not have an active subscription eligible for refund.",
                ),
            )

    if err := _check_refund_window(acct):
        return RefundOutput(success=False, error=err)

    if err := _check_refund_lifetime(acct):
        return RefundOutput(success=False, escalation_required=True, error=err)

    # Refund covers plan price only — extra fees are non-refundable.
    refund_amount = acct.plan_price
    refund_method = "original payment method"
    has_guest = acct.plan == "black_card"

    if not input.confirmed:
        payload = ConfirmationPayload(
            action="process_refund",
            summary=f"Full refund of €{refund_amount:.2f} for account {input.account_id}.",
            billing_impact=f"€{refund_amount:.2f} refunded to {refund_method}. Credit expected within 3–10 business days.",
            member_impact="Subscription will be invalidated immediately upon refund.",
            guest_pass_impact=(
                "Guest pass will be invalidated immediately."
                if has_guest
                else "N/A — Pass plan has no guest pass."
            ),
            subscription_validity_impact="Subscription ends immediately. Cannot be reinstated without a new purchase.",
            raw={
                "account_id": input.account_id,
                "refund_amount": refund_amount,
                "method": refund_method,
                "plan": acct.plan,
                "billing_cycle": acct.billing_cycle,
            },
        )
        return RefundOutput(
            success=False,
            refund_amount=refund_amount,
            currency="EUR",
            refund_method=refund_method,
            subscription_status=acct.status,
            guest_pass_status="will_be_invalidated" if has_guest else None,
            confirmation_required=True,
            confirmation_payload=payload,
        )

    # ── CONFIRMED — execute ──────────────────────────────────────────────────
    # DEMO STUB: replace with → POST /accounts/{account_id}/refund
    # Expected API payload: {"amount": refund_amount, "method": refund_method}
    return RefundOutput(
        success=True,
        refund_amount=refund_amount,
        currency="EUR",
        refund_method=refund_method,
        processing_timeline="3–10 business days",
        subscription_status="invalidated",
        guest_pass_status="invalidated" if has_guest else None,
    )


# ─────────────────────────────────────────────────────────────────────────────
# TOOL 5: Cancel
# ─────────────────────────────────────────────────────────────────────────────

class CancelInput(BaseModel):
    account_id: str = Field(description="Member account ID.")
    session_id: str = Field(description="Active session ID.")
    confirmed: bool = Field(default=False, description="Set True after member confirms.")


class CancelOutput(BaseModel):
    success: bool
    auto_renewal_after: bool | None = None
    subscription_valid_until: str | None = None
    guest_pass_valid_until: str | None = None
    confirmation_required: bool = False
    confirmation_payload: ConfirmationPayload | None = None
    error: ToolError | None = None


@register_tool
def cancel_subscription(input: CancelInput) -> CancelOutput:
    """
    Cancel a subscription by setting autoRenewal=false.

    Effects:
      - autoRenewal set to false
      - Subscription remains active until prepaid period ends (no refund, no early termination)
      - Guest pass also valid until period end (Black Card)

    Reversible: autoRenewal can be re-enabled before period end.
    Blocked if: pending operation active, no active subscription.
    Note: does NOT block a subsequent refund if within 14-day window.

    First call (confirmed=False): returns confirmation_payload.
    Second call (confirmed=True): executes the cancellation.
    """
    acct = _get_account(input.account_id)
    if isinstance(acct, ToolError):
        return CancelOutput(success=False, error=acct)

    if err := _block_if_pending(acct):
        return CancelOutput(success=False, error=err)

    if err := _require_active_subscription(acct):
        return CancelOutput(success=False, error=err)

    if not acct.auto_renewal:
        return CancelOutput(
            success=False,
            error=ToolError(
                code="ALREADY_CANCELLED",
                message="Auto-renewal is already disabled for this account.",
                details={"subscription_valid_until": acct.subscription_valid_until.isoformat()},
            ),
        )

    valid_until = acct.subscription_valid_until.isoformat()
    has_guest = acct.plan == "black_card"

    if not input.confirmed:
        payload = ConfirmationPayload(
            action="cancel_subscription",
            summary=f"Cancel subscription for account {input.account_id} — auto-renewal will be disabled.",
            billing_impact="No payment returned. No further charges after the current period.",
            member_impact=f"Subscription remains active until {valid_until}.",
            guest_pass_impact=(
                f"Guest pass remains valid until {valid_until}, then invalidated."
                if has_guest
                else "N/A — Pass plan has no guest pass."
            ),
            subscription_validity_impact=f"Subscription expires at end of prepaid period ({valid_until}).",
            raw={"account_id": input.account_id, "valid_until": valid_until},
        )
        return CancelOutput(
            success=False,
            auto_renewal_after=False,
            subscription_valid_until=valid_until,
            guest_pass_valid_until=valid_until if has_guest else None,
            confirmation_required=True,
            confirmation_payload=payload,
        )

    # ── CONFIRMED — execute ──────────────────────────────────────────────────
    # DEMO STUB: replace with → PATCH /accounts/{account_id} {"auto_renewal": false}
    return CancelOutput(
        success=True,
        auto_renewal_after=False,
        subscription_valid_until=valid_until,
        guest_pass_valid_until=valid_until if has_guest else None,
    )


# ─────────────────────────────────────────────────────────────────────────────
# TOOL 6: Pause
# ─────────────────────────────────────────────────────────────────────────────

class PauseInput(BaseModel):
    account_id: str = Field(description="Member account ID.")
    session_id: str = Field(description="Active session ID.")
    start_date: date = Field(description="Pause start date. Must be ≥14 days from today.")
    duration_days: int = Field(description="Pause duration in days (1–180 max).")
    end_date: date | None = Field(
        default=None,
        description="Pause end date. Derived as start_date + duration_days if omitted.",
    )
    has_documentation: bool = Field(
        default=False,
        description=(
            "True if the member has valid fee-free documentation "
            "(medical, military, or job displacement). "
            "Document submission via support@fitness.com is handled separately."
        ),
    )
    confirmed: bool = Field(default=False, description="Set True after member confirms.")

    @model_validator(mode="after")
    def reconcile_dates(self) -> "PauseInput":
        """Ensure start_date, duration_days, end_date are consistent.
        If end_date is omitted, derive it. If all three are present and inconsistent,
        start_date and end_date take priority and duration_days is recalculated."""
        if self.end_date is None:
            self.end_date = self.start_date + timedelta(days=self.duration_days)
        else:
            derived_days = (self.end_date - self.start_date).days
            if derived_days != self.duration_days:
                self.duration_days = derived_days
        return self


class PauseOutput(BaseModel):
    success: bool
    pause_fee: float | None = None
    pause_start: str | None = None
    pause_end: str | None = None
    duration_days: int | None = None
    new_renewal_date: str | None = None
    guest_pass_impact: str | None = None
    escalation_required: bool = False
    confirmation_required: bool = False
    confirmation_payload: ConfirmationPayload | None = None
    error: ToolError | None = None


@register_tool
def pause_subscription(input: PauseInput) -> PauseOutput:
    """
    Pause a member's subscription for a pre-defined duration.

    Eligibility:
      - No pending operation
      - Active subscription, not already paused
      - Request submitted ≥14 days before start_date
      - Duration: 1–90 days (fee applies), 91–180 days (escalate), 181+ denied
      - has_documentation=True: always escalated — staff validate docs before pause is applied

    Fee waiver documentation: medical condition, military service, or job displacement.
    Must include signature, date, confirmation gym attendance is problematic, period end date.
    Submission: support@fitness.com. Pause is applied by staff after validation.

    Effects: billing cycle delayed by duration_days; all features suspended; guest pass also paused (Black Card).
    Duration is fixed — cannot be extended once pause is active.

    First call (confirmed=False): returns confirmation_payload.
    Second call (confirmed=True): executes the pause.
    """
    acct = _get_account(input.account_id)
    if isinstance(acct, ToolError):
        return PauseOutput(success=False, error=acct)

    if err := _block_if_pending(acct):
        return PauseOutput(success=False, error=err)

    if err := _require_active_subscription(acct):
        return PauseOutput(success=False, error=err)

    if acct.is_paused:
        return PauseOutput(
            success=False,
            error=ToolError(
                code="ALREADY_PAUSED",
                message="This account already has an active pause.",
            ),
        )

    if err := _check_pause_notice(input.start_date):
        return PauseOutput(success=False, error=err)

    if err := _check_pause_duration(input.duration_days, input.has_documentation):
        return PauseOutput(
            success=False,
            escalation_required=err.code in (
                "PAUSE_ESCALATION_REQUIRED", "PAUSE_DOCUMENTATION_ESCALATION"
            ),
            error=err,
        )

    # end_date guaranteed by model_validator (start_date + duration_days)
    pause_end = input.end_date  # type: ignore[assignment]
    new_renewal = acct.subscription_valid_until + timedelta(days=input.duration_days)
    fee = _pause_fee(input.duration_days)
    has_guest = acct.plan == "black_card"
    periods = math.ceil(input.duration_days / 30)

    fee_str = f"€{fee:.2f} (€{PAUSE_FEE_PER_MONTH:.0f}/30-day period × {periods} period(s))"
    guest_note = (
        f"Guest pass paused from {input.start_date.isoformat()} to {pause_end.isoformat()}."
        if has_guest
        else "N/A — Pass plan has no guest pass."
    )

    if not input.confirmed:
        payload = ConfirmationPayload(
            action="pause_subscription",
            summary=(
                f"{input.duration_days}-day pause "
                f"({input.start_date.isoformat()} → {pause_end.isoformat()}) "
                f"for account {input.account_id}."
            ),
            billing_impact=f"Pause fee: {fee_str}. Renewal date pushed to {new_renewal.isoformat()}.",
            member_impact="All subscription features suspended during the pause period.",
            guest_pass_impact=guest_note,
            subscription_validity_impact=f"Renewal date shifted to {new_renewal.isoformat()}.",
            raw={
                "account_id": input.account_id,
                "start_date": input.start_date.isoformat(),
                "end_date": pause_end.isoformat(),
                "duration_days": input.duration_days,
                "pause_fee": fee,
                "new_renewal_date": new_renewal.isoformat(),
            },
        )
        return PauseOutput(
            success=False,
            pause_fee=fee,
            pause_start=input.start_date.isoformat(),
            pause_end=pause_end.isoformat(),
            duration_days=input.duration_days,
            new_renewal_date=new_renewal.isoformat(),
            guest_pass_impact=guest_note,
            confirmation_required=True,
            confirmation_payload=payload,
        )

    # ── CONFIRMED — execute ──────────────────────────────────────────────────
    # DEMO STUB: replace with → POST /accounts/{account_id}/pause
    # Expected payload: {"start_date": ..., "end_date": ..., "duration_days": ..., "fee": fee}
    return PauseOutput(
        success=True,
        pause_fee=fee,
        pause_start=input.start_date.isoformat(),
        pause_end=pause_end.isoformat(),
        duration_days=input.duration_days,
        new_renewal_date=new_renewal.isoformat(),
        guest_pass_impact=guest_note,
    )


# ─────────────────────────────────────────────────────────────────────────────
# TOOL 7: Plan Change
# ─────────────────────────────────────────────────────────────────────────────

class PlanChangeInput(BaseModel):
    account_id: str = Field(description="Member account ID.")
    session_id: str = Field(description="Active session ID.")
    new_plan: str = Field(description="Target plan: 'pass' or 'black_card'.")
    new_billing_type: str = Field(description="Target billing cycle: 'monthly' or 'annual'.")
    confirmed: bool = Field(default=False, description="Set True after member confirms.")


class PlanChangeOutput(BaseModel):
    success: bool
    change_type: str | None = None           # "upgrade" | "downgrade"
    from_plan: str | None = None
    to_plan: str | None = None
    immediate_charge: float | None = None    # upgrade only
    effective_date: str | None = None        # upgrade: today | downgrade: period end
    guest_pass_impact: str | None = None
    confirmation_required: bool = False
    confirmation_payload: ConfirmationPayload | None = None
    error: ToolError | None = None


@register_tool
def change_plan(input: PlanChangeInput) -> PlanChangeOutput:
    """
    Change a member's plan or billing cycle.

    Upgrade (Pass → Black Card, or monthly → annual at higher plan value):
      - Delta billing: charge = new_price − (days_remaining × current_daily_rate)
      - Effective immediately
      - Guest pass issued immediately (if upgrading to Black Card)

    Downgrade (Black Card → Pass, or annual → monthly at lower plan value):
      - No immediate charge
      - Scheduled at end of current prepaid period
      - Guest pass valid until period end, then invalidated

    Blocked if: pending operation active, no active subscription, account is paused.
    No cooldown between plan changes.

    First call (confirmed=False): returns confirmation_payload.
    Second call (confirmed=True): executes the change.
    """
    acct = _get_account(input.account_id)
    if isinstance(acct, ToolError):
        return PlanChangeOutput(success=False, error=acct)

    if err := _block_if_pending(acct):
        return PlanChangeOutput(success=False, error=err)

    if err := _require_active_subscription(acct):
        return PlanChangeOutput(success=False, error=err)

    if err := _block_if_paused(acct):
        return PlanChangeOutput(success=False, error=err)

    # Validate inputs
    if input.new_plan not in _PLAN_PRICES:
        return PlanChangeOutput(
            success=False,
            error=ToolError(
                code="INVALID_PLAN",
                message=f"Unknown plan '{input.new_plan}'. Valid: pass, black_card.",
            ),
        )
    if input.new_billing_type not in ("monthly", "annual"):
        return PlanChangeOutput(
            success=False,
            error=ToolError(
                code="INVALID_BILLING_TYPE",
                message="billing_type must be 'monthly' or 'annual'.",
            ),
        )

    current_value = _PLAN_PRICES[acct.plan][acct.billing_cycle]
    new_value = _PLAN_PRICES[input.new_plan][input.new_billing_type]
    is_upgrade = new_value > current_value
    from_label = f"{acct.plan}/{acct.billing_cycle}"
    to_label   = f"{input.new_plan}/{input.new_billing_type}"

    if is_upgrade:
        charge = _upgrade_delta_charge(acct, input.new_billing_type)
        effective_date = date.today().isoformat()
        change_type = "upgrade"
        guest_note = (
            "Guest pass issued immediately."
            if input.new_plan == "black_card"
            else "N/A — Pass plan has no guest pass."
        )
        billing_note = f"Immediate delta charge: €{charge:.2f}."
        member_note  = "Plan change effective immediately."
        validity_note = f"Subscription continues under new plan from {effective_date}."
    else:
        charge = 0.0
        effective_date = acct.subscription_valid_until.isoformat()
        change_type = "downgrade"
        guest_note = (
            f"Guest pass valid until {effective_date}, then invalidated."
            if acct.plan == "black_card"
            else "N/A — Pass plan has no guest pass."
        )
        billing_note = f"No charge now. Change takes effect at period end ({effective_date})."
        member_note  = f"Downgrade scheduled for {effective_date}. Current plan active until then."
        validity_note = f"Current plan valid until {effective_date}; new plan active from {effective_date}."

    if not input.confirmed:
        payload = ConfirmationPayload(
            action="change_plan",
            summary=f"{change_type.capitalize()}: {from_label} → {to_label} for account {input.account_id}.",
            billing_impact=billing_note,
            member_impact=member_note,
            guest_pass_impact=guest_note,
            subscription_validity_impact=validity_note,
            raw={
                "account_id": input.account_id,
                "from_plan": acct.plan,
                "from_billing_cycle": acct.billing_cycle,
                "to_plan": input.new_plan,
                "to_billing_cycle": input.new_billing_type,
                "change_type": change_type,
                "charge": charge,
                "effective_date": effective_date,
            },
        )
        return PlanChangeOutput(
            success=False,
            change_type=change_type,
            from_plan=from_label,
            to_plan=to_label,
            immediate_charge=charge if is_upgrade else None,
            effective_date=effective_date,
            guest_pass_impact=guest_note,
            confirmation_required=True,
            confirmation_payload=payload,
        )

    # ── CONFIRMED — execute ──────────────────────────────────────────────────
    # DEMO STUB: replace with → PATCH /accounts/{account_id}/plan
    # Expected payload: {"new_plan": input.new_plan, "new_billing_type": input.new_billing_type}
    return PlanChangeOutput(
        success=True,
        change_type=change_type,
        from_plan=from_label,
        to_plan=to_label,
        immediate_charge=charge if is_upgrade else None,
        effective_date=effective_date,
        guest_pass_impact=guest_note,
    )
