"""
tools/appointments.py

Appointment & scheduling domain tool implementations.

Five tools:
  appointment_faq          — answer service/policy questions
  check_availability       — list open slots
  book_appointment         — book a new session (requires confirmation)
  reschedule_appointment   — move an existing booking (requires confirmation)
  cancel_appointment       — cancel a booking; late fee enforced (requires confirmation)

Architecture rule enforced here:
  All policy constraints live in this file — not in the planner.
  The planner selects tools. Tools enforce their own eligibility rules.

DEMO LAYER:
  _MOCK_APPOINTMENTS contains six fixture clients with relative dates.
  _get_appointment() merges DB overrides on top of the fixture.
  In production: replace _get_appointment() with a real API call.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

from pydantic import BaseModel, Field, model_validator

from config import llm_call
from db import crud
from tools.registry import register_tool

# ─── Policy constants ─────────────────────────────────────────────────────────

CANCELLATION_FEE = 30.0             # same-day cancellation fee (€)
RESCHEDULE_FEE = 15.0               # same-day rescheduling fee (€)
MAX_ADVANCE_BOOKING_DAYS = 60       # cannot book more than 60 days out
MAX_SLOT_SUGGESTIONS = 4            # max available dates returned; note emitted if more exist

CLOSED_WEEKDAY = 6                  # Sunday (0=Monday … 6=Sunday)

SERVICES: dict[str, dict[str, Any]] = {
    "initial_consultation": {"name": "Initial Consultation"},
    "follow_up":            {"name": "Follow-up Session"},
    "assessment":           {"name": "Assessment"},
    "coaching":             {"name": "Coaching Session"},
}

# ─── FAQ knowledge base ───────────────────────────────────────────────────────

_FAQ_CONTENT = """\
Services:
- Initial Consultation: first session for new clients — includes a full needs assessment.
- Follow-up Session: for existing clients continuing their programme.
- Assessment: comprehensive evaluation and personalised action plan.
- Coaching Session: one-on-one coaching tailored to your goals.

Booking model:
- Appointments are full-day bookings. There are no specific time slots — the client picks a date, not a time.
- Do not mention or invent times (e.g. 9:00 AM, 11:00, 14:00) — none exist in this system.

Opening hours:
- Monday to Saturday.
- Sunday: closed.

Cancellation policy:
- Cancellations before the day of your appointment: free of charge.
- Same-day cancellations: €30 fee.
- No-shows: charged the full session fee.

Rescheduling policy:
- Rescheduling before the day of your appointment: free of charge.
- Same-day rescheduling: €15 fee.

Booking rules:
- You can book up to 60 days in advance.
- Same-day bookings are welcome, subject to availability.

Payment:
- Due at the time of your appointment.
- Accepted methods: card, bank transfer.
"""

_FAQ_SYSTEM = (
    "You are an appointment desk assistant. Answer the client's question using only "
    "the information below. Be concise — 1–3 sentences. No filler phrases.\n\n"
    + _FAQ_CONTENT
)


# ─── Shared result primitives ─────────────────────────────────────────────────

class ToolError(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class AppointmentConfirmationPayload(BaseModel):
    action: str
    summary: str
    fee_impact: str
    appointment_date: str | None = None
    service: str | None = None
    old_date: str | None = None
    new_date: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


# ─── Appointment state model ──────────────────────────────────────────────────

class AppointmentState(BaseModel):
    """
    Persisted client appointment data.
    In production: replace with a real booking system API call.

    status: "scheduled" | "cancelled" | "completed" | "no_show" | "pending_reschedule" | "none"
    """
    client_id: str
    client_name: str
    service_type: str | None = None     # key from SERVICES
    appointment_date: date | None = None
    status: str = "none"
    has_pending_action: bool = False
    notes: str | None = None


# ─── Demo archetypes (public) ─────────────────────────────────────────────────

ARCHETYPES: list[dict[str, Any]] = [
    {
        "id": "client-reschedule-ok",
        "label": "Sophie · Follow-up · early next week",
        "tags": ["Free reschedule", "24h+ notice"],
        "description": "Has a session early next week. Rescheduling or cancelling is free of charge.",
    },
    {
        "id": "client-cancel-sameday",
        "label": "Marco · Assessment · Today",
        "tags": ["Same-day cancel", "€30 fee applies"],
        "description": "Appointment is today. A late cancellation fee of €30 applies.",
    },
    {
        "id": "client-new",
        "label": "Amelia · New client · No booking",
        "tags": ["No appointment", "Ready to book"],
        "description": "New client with no existing appointment. Try booking a first session.",
    },
    {
        "id": "client-cancel-ok",
        "label": "Thomas · Consultation · later this week",
        "tags": ["Free cancel", "24h+ notice"],
        "description": "Consultation later this week. Can cancel or reschedule free of charge.",
    },
]


# ─── Mock appointment data ────────────────────────────────────────────────────
# Dates are computed relative to today so the demo is always fresh.

def _weekday_date(base: date, min_days: int) -> date:
    """Return the first non-Sunday date that is at least min_days after base."""
    d = base + timedelta(days=min_days)
    while d.weekday() == CLOSED_WEEKDAY:
        d += timedelta(days=1)
    return d


def _build_mock_appointments() -> dict[str, dict[str, Any]]:
    today = date.today()
    return {
        "client-reschedule-ok": {
            "client_id": "client-reschedule-ok",
            "client_name": "Sophie Bernard",
            "service_type": "follow_up",
            "appointment_date": _weekday_date(today, 3).isoformat(),
            "status": "scheduled",
            "has_pending_action": False,
        },
        "client-cancel-sameday": {
            "client_id": "client-cancel-sameday",
            "client_name": "Marco Rossi",
            "service_type": "assessment",
            "appointment_date": today.isoformat(),
            "status": "scheduled",
            "has_pending_action": False,
        },
        "client-new": {
            "client_id": "client-new",
            "client_name": "Amelia Chen",
            "service_type": None,
            "appointment_date": None,
            "status": "none",
            "has_pending_action": False,
        },
        "client-cancel-ok": {
            "client_id": "client-cancel-ok",
            "client_name": "Thomas Müller",
            "service_type": "initial_consultation",
            "appointment_date": _weekday_date(today, 5).isoformat(),
            "status": "scheduled",
            "has_pending_action": False,
        },
    }


# ─── Mock data access ─────────────────────────────────────────────────────────

def _get_appointment(client_id: str) -> AppointmentState | ToolError:
    """Load client appointment from fixture, merged with any DB override."""
    fixture = _build_mock_appointments().get(client_id)
    if fixture is None:
        return ToolError(
            code="APPOINTMENT_NOT_FOUND",
            message=f"No client found with ID '{client_id}'.",
        )
    override = crud.get_demo_account_state(client_id)
    data: dict[str, Any] = {**fixture, **(override or {})}

    # Deserialise date strings stored by crud
    raw_date = data.get("appointment_date")
    if raw_date and isinstance(raw_date, str):
        try:
            data["appointment_date"] = date.fromisoformat(raw_date)
        except (ValueError, TypeError):
            data["appointment_date"] = None

    return AppointmentState(**{k: v for k, v in data.items() if k in AppointmentState.model_fields})


def _persist_appointment(client_id: str, state: AppointmentState) -> None:
    crud.set_demo_account_state(client_id, state.model_dump(mode="json"))


# ─── Manual busy-date store (demo only) ───────────────────────────────────────
# Frontend calendar can mark days as manually booked; the set is held in memory
# and checked by _available_slots_on so the agent respects manual blocks.

_MANUAL_BUSY_DATES: set[str] = set()


def set_manual_busy_dates(dates: list[str]) -> None:
    """Replace the full set of manually blocked dates. Called by POST /db/busy-dates."""
    global _MANUAL_BUSY_DATES
    _MANUAL_BUSY_DATES = set(dates)


def clear_manual_busy_dates() -> None:
    global _MANUAL_BUSY_DATES
    _MANUAL_BUSY_DATES = set()


# ─── Schedule helpers ─────────────────────────────────────────────────────────

def _is_day_booked(target_date: date) -> bool:
    """Return True if any client has a scheduled appointment on target_date."""
    for client_id, fixture in _build_mock_appointments().items():
        override = crud.get_demo_account_state(client_id)
        data = {**fixture, **(override or {})}
        if data.get("status") != "scheduled" or not data.get("appointment_date"):
            continue
        try:
            appt_date = (
                date.fromisoformat(data["appointment_date"])
                if isinstance(data["appointment_date"], str)
                else data["appointment_date"]
            )
        except (ValueError, TypeError):
            continue
        if appt_date == target_date:
            return True
    return False


def _is_available_on(target_date: date) -> bool:
    """Return True if target_date is open for booking."""
    if target_date.weekday() == CLOSED_WEEKDAY:
        return False
    if target_date.isoformat() in _MANUAL_BUSY_DATES:
        return False
    return not _is_day_booked(target_date)


def _available_dates_in_range(start: date, end: date) -> list[date]:
    """Return all open days in [start, end], sorted ascending."""
    results: list[date] = []
    d = start
    while d <= end:
        if _is_available_on(d):
            results.append(d)
        d += timedelta(days=1)
    return results


def _nearest_outside_range(start: date, end: date) -> list[date]:
    """
    Find the nearest available day before start (left) and after end (right).
    Returns up to 2 candidates ordered by distance; tie → earlier comes first.
    """
    today = date.today()
    horizon = today + timedelta(days=MAX_ADVANCE_BOOKING_DAYS)

    left: date | None = None
    d = start - timedelta(days=1)
    while d >= today:
        if _is_available_on(d):
            left = d
            break
        d -= timedelta(days=1)

    right: date | None = None
    d = end + timedelta(days=1)
    while d <= horizon:
        if _is_available_on(d):
            right = d
            break
        d += timedelta(days=1)

    if left and right:
        dist_left = (start - left).days
        dist_right = (right - end).days
        if dist_right < dist_left:
            return [right, left]
        return [left, right]
    return [c for c in [left, right] if c is not None]



# ─── Calendar query helper ────────────────────────────────────────────────────

def get_scheduled_appointments() -> list[dict[str, Any]]:
    """Return all active appointments with dates, for calendar display."""
    results = []
    for client_id, fixture in _build_mock_appointments().items():
        override = crud.get_demo_account_state(client_id)
        data = {**fixture, **(override or {})}
        if data.get("status") != "scheduled":
            continue
        raw_date = data.get("appointment_date")
        if not raw_date:
            continue
        try:
            appt_date = (
                date.fromisoformat(raw_date) if isinstance(raw_date, str) else raw_date
            )
        except (ValueError, TypeError):
            continue
        results.append({
            "account_id": client_id,
            "client_name": data.get("client_name", ""),
            "appointment_date": appt_date.isoformat(),
            "status": data.get("status", ""),
        })
    return results


# ─── describe_entity (called by core/planner.py for context injection) ────────

def describe_entity(client_id: str) -> str | None:
    """Return a concise LLM-readable summary of a client's appointment status."""
    today = date.today()
    appt = _get_appointment(client_id)
    if isinstance(appt, ToolError):
        return None
    name = appt.client_name
    today_str = today.strftime("%A, %d %B %Y")

    if appt.status == "none":
        return f"Today: {today_str}. Client: {name}. No existing appointment."
    if appt.status == "scheduled" and appt.appointment_date:
        svc = SERVICES.get(appt.service_type or "", {}).get("name", appt.service_type or "Appointment")
        delta = (appt.appointment_date - today).days
        when = (
            "today" if delta == 0
            else "tomorrow" if delta == 1
            else f"in {delta} days ({appt.appointment_date.strftime('%A, %d %B')})"
        )
        return (
            f"Today: {today_str}. Client: {name}. "
            f"{svc} scheduled {when}. Status: active."
        )
    if appt.status == "no_show":
        return f"Today: {today_str}. Client: {name}. Missed appointment yesterday. Status: no-show."
    if appt.status == "cancelled":
        return f"Today: {today_str}. Client: {name}. No active appointment (previously cancelled)."
    if appt.status == "pending_reschedule" and appt.appointment_date:
        return (
            f"Today: {today_str}. Client: {name}. "
            f"Appointment on {appt.appointment_date.strftime('%A, %d %B')} — "
            f"reschedule in progress. Status: pending."
        )
    return f"Today: {today_str}. Client: {name}. Status: {appt.status}."


# ─── Tool input/output models ─────────────────────────────────────────────────

class AppointmentFAQInput(BaseModel):
    question: str = Field(description="The client's question about services, policies, or availability.")
    session_id: str


class FAQResult(BaseModel):
    answer: str
    session_id: str


class CheckAvailabilityInput(BaseModel):
    date_range_start: str | None = Field(
        default=None,
        description=(
            "Start of the requested date range (YYYY-MM-DD). "
            "For a single day query, set this and leave date_range_end unset. "
            "If both are omitted, returns the next available days."
        ),
    )
    date_range_end: str | None = Field(
        default=None,
        description=(
            "End of the requested date range (YYYY-MM-DD, inclusive). "
            "Set when the client specifies a range such as 'next week' or 'this month'. "
            "Leave unset for single-day queries."
        ),
    )
    session_id: str

    @model_validator(mode="before")
    @classmethod
    def _normalise_date_fields(cls, data: dict) -> dict:
        """Accept any start/end field name the LLM might produce."""
        if not isinstance(data, dict):
            return data
        _START = {"date_range_start", "range_start", "date_start", "start_date", "start", "from_date", "from"}
        _END   = {"date_range_end",   "range_end",   "date_end",   "end_date",   "end",   "to_date",   "to"}
        for k, v in list(data.items()):
            if k in _START and "date_range_start" not in data:
                data["date_range_start"] = v
            elif k in _END and "date_range_end" not in data:
                data["date_range_end"] = v
        return data


class AvailabilityResult(BaseModel):
    available_dates: list[str]
    message: str
    has_more: bool = False
    total_in_range: int = 0
    session_id: str


class BookAppointmentInput(BaseModel):
    account_id: str = Field(description="Client ID (injected from account context — do not extract from message).")
    requested_date: str | None = Field(default=None, description="Requested appointment date (YYYY-MM-DD). Omit if the client has not specified a date.")
    session_id: str
    confirmed: bool | None = None


class BookResult(BaseModel):
    success: bool = False
    confirmation_required: bool = False
    confirmation_payload: AppointmentConfirmationPayload | None = None
    appointment_date: str | None = None
    alternatives: list[dict[str, Any]] | None = None  # nearest open days when requested date is full
    message: str | None = None  # pre-formed response (e.g. when no date was given)
    error: ToolError | None = None
    debug: dict[str, Any] | None = None


class RescheduleAppointmentInput(BaseModel):
    account_id: str = Field(description="Client ID (injected from account context — do not extract from message).")
    new_date: str = Field(description="New appointment date (YYYY-MM-DD).")
    session_id: str
    confirmed: bool | None = None


class RescheduleResult(BaseModel):
    success: bool = False
    confirmation_required: bool = False
    confirmation_payload: AppointmentConfirmationPayload | None = None
    old_date: str | None = None
    new_date: str | None = None
    reschedule_fee: float | None = None
    alternatives: list[dict[str, Any]] | None = None  # nearest open days when requested date is full
    error: ToolError | None = None
    debug: dict[str, Any] | None = None


class CancelAppointmentInput(BaseModel):
    account_id: str = Field(description="Client ID (injected from account context — do not extract from message).")
    session_id: str
    confirmed: bool | None = None


class CancelResult(BaseModel):
    success: bool = False
    confirmation_required: bool = False
    confirmation_payload: AppointmentConfirmationPayload | None = None
    appointment_date: str | None = None
    cancellation_fee: float | None = None
    fee_waived: bool = False
    error: ToolError | None = None
    debug: dict[str, Any] | None = None


# ─── Tool 1: FAQ ──────────────────────────────────────────────────────────────

@register_tool
def appointment_faq(input: AppointmentFAQInput) -> FAQResult:
    """Answer questions about services, pricing, and booking policies."""
    resp = llm_call(
        messages=[{"role": "user", "content": input.question}],
        system=_FAQ_SYSTEM,
        model_tier="fast",
    )
    answer = resp.get("content", "").strip() or "Please contact us directly for assistance."
    return FAQResult(answer=answer, session_id=input.session_id)


# ─── Tool 2: Check availability ───────────────────────────────────────────────

@register_tool
def check_availability(input: CheckAvailabilityInput) -> AvailabilityResult:
    """Check available appointment days within a date or date range."""
    today = date.today()

    def _fmt(d: date) -> str:
        return d.strftime("%A, %d %B").replace(" 0", " ")

    if input.date_range_start:
        try:
            date_range_start = date.fromisoformat(input.date_range_start)
        except ValueError:
            date_range_start = today

        date_range_start = max(date_range_start, today)

        try:
            date_range_end = date.fromisoformat(input.date_range_end) if input.date_range_end else date_range_start
        except ValueError:
            date_range_end = date_range_start

        if date_range_end < date_range_start:
            date_range_end = date_range_start

        available = _available_dates_in_range(date_range_start, date_range_end)
        total = len(available)

        if total:
            capped = available[:MAX_SLOT_SUGGESTIONS]
            has_more = total > MAX_SLOT_SUGGESTIONS
            dates_out = [d.isoformat() for d in capped]
            summary = ", ".join(_fmt(d) for d in capped)
            msg = f"We have availability on: {summary}."
            if has_more:
                remaining = total - MAX_SLOT_SUGGESTIONS
                msg += f" There {'are' if remaining > 1 else 'is'} {remaining} more day{'s' if remaining > 1 else ''} open in this period too."
            return AvailabilityResult(
                available_dates=dates_out,
                message=msg,
                has_more=has_more,
                total_in_range=total,
                session_id=input.session_id,
            )

        # Nothing in range — find nearest outside
        range_str = (
            f"{date_range_start.strftime('%d %B').replace(' 0', ' ')}–{date_range_end.strftime('%d %B').replace(' 0', ' ')}"
            if date_range_start != date_range_end
            else date_range_start.strftime("%A, %d %B").replace(" 0", " ")
        )
        nearest = _nearest_outside_range(date_range_start, date_range_end)
        dates_out = [d.isoformat() for d in nearest]

        if nearest:
            alts = " or ".join(_fmt(d) for d in nearest)
            msg = f"Sorry, we're fully booked from {range_str}. The nearest available date is {alts} — would you like to book that?"
        else:
            msg = f"Sorry, we don't have any openings around {range_str}. Please contact us directly and we'll find something that works."

        return AvailabilityResult(
            available_dates=dates_out,
            message=msg,
            has_more=False,
            total_in_range=0,
            session_id=input.session_id,
        )

    # No date specified — return next available days (up to MAX_SLOT_SUGGESTIONS)
    available_: list[date] = []
    d = today
    limit = today + timedelta(days=14)
    while len(available_) < MAX_SLOT_SUGGESTIONS and d <= limit:
        if _is_available_on(d):
            available_.append(d)
        d += timedelta(days=1)

    dates_out = [d.isoformat() for d in available_]

    if available_:
        summary = ", ".join(_fmt(d) for d in available_)
        msg = f"Here are our next available dates: {summary}. Which would you prefer?"
    else:
        msg = "Sorry, we have no openings in the next two weeks. Please contact us directly and we'll sort something out."

    return AvailabilityResult(
        available_dates=dates_out,
        message=msg,
        session_id=input.session_id,
    )


# ─── Tool 3: Book appointment ────────────────────────────────────────────────

@register_tool
def book_appointment(input: BookAppointmentInput) -> BookResult:
    """Book a new appointment for a client. Requires confirmation before executing."""
    client_id = input.account_id

    # ── No date specified — show next available slots ─────────────────────────
    if not input.requested_date:
        today = date.today()
        available: list[date] = []
        d = today
        limit = today + timedelta(days=MAX_ADVANCE_BOOKING_DAYS)
        while len(available) < MAX_SLOT_SUGGESTIONS and d <= limit:
            if _is_available_on(d):
                available.append(d)
            d += timedelta(days=1)
        if available:
            options = ", ".join(a.strftime("%A %d %B") for a in available)
            msg = f"Of course! Here are our next available dates: {options}. Which would you prefer?"
        else:
            msg = "Sorry, we have no availability in the coming weeks. Please contact us to arrange something."
        return BookResult(
            message=msg,
            alternatives=[{"date": a.isoformat(), "day": a.strftime("%A")} for a in available] or None,
        )

    # ── Policy checks ────────────────────────────────────────────────────────
    appt = _get_appointment(client_id)
    if isinstance(appt, ToolError):
        return BookResult(error=appt)

    if appt.status == "scheduled":
        date_str = appt.appointment_date.strftime("%A, %d %B") if appt.appointment_date else "a future date"
        return BookResult(error=ToolError(
            code="DUPLICATE_BOOKING",
            message=(
                f"An appointment is already scheduled for {date_str}. "
                "Please cancel or reschedule the existing appointment first."
            ),
        ))

    if appt.has_pending_action:
        return BookResult(error=ToolError(
            code="PENDING_RESCHEDULE_ACTIVE",
            message="There is a pending action on this account. Please resolve it before booking.",
        ))

    try:
        requested = date.fromisoformat(input.requested_date or "")
    except ValueError:
        return BookResult(error=ToolError(
            code="INVALID_DATE",
            message=f"Could not parse date '{input.requested_date}'. Use YYYY-MM-DD format.",
        ))

    today = date.today()
    if requested < today:
        return BookResult(error=ToolError(
            code="DATE_IN_PAST",
            message="The requested date is in the past. Please choose a future date.",
        ))

    if (requested - today).days > MAX_ADVANCE_BOOKING_DAYS:
        return BookResult(error=ToolError(
            code="MAX_ADVANCE_BOOKING_EXCEEDED",
            message=f"Bookings can only be made up to {MAX_ADVANCE_BOOKING_DAYS} days in advance.",
        ))

    if requested.weekday() == CLOSED_WEEKDAY:
        return BookResult(error=ToolError(
            code="CLOSED_DAY",
            message="We are closed on Sundays. Please choose a Monday–Saturday date.",
        ))

    if not _is_available_on(requested):
        nearest = _nearest_outside_range(requested, requested)
        alts = [{"date": d.isoformat(), "day": d.strftime("%A")} for d in nearest]
        alt_str = " or ".join(f"{a['day']} {a['date']}" for a in alts) if alts else "another date"
        return BookResult(
            alternatives=alts or None,
            error=ToolError(
                code="DATE_NOT_AVAILABLE",
                message=(
                    f"{requested.strftime('%A, %d %B')} is not available. "
                    f"Nearest available: {alt_str}."
                ),
            ),
        )

    # ── Return confirmation request on first call ────────────────────────────
    if not input.confirmed:
        payload = AppointmentConfirmationPayload(
            action="book_appointment",
            summary=f"Book appointment on {requested.strftime('%A, %d %B')}.",
            fee_impact="Payment due at appointment",
            appointment_date=requested.strftime("%A, %d %B"),
            raw={},
        )
        return BookResult(
            confirmation_required=True,
            confirmation_payload=payload,
            appointment_date=requested.isoformat(),
        )

    # ── Execute on confirmed=True ────────────────────────────────────────────
    new_state = AppointmentState(
        client_id=client_id,
        client_name=appt.client_name,
        appointment_date=requested,
        status="scheduled",
        has_pending_action=False,
    )
    _persist_appointment(client_id, new_state)

    return BookResult(
        success=True,
        appointment_date=requested.isoformat(),
        message=f"Your appointment has been booked for {requested.strftime('%A, %d %B')}. See you then!",
    )


# ─── Tool 4: Reschedule appointment ─────────────────────────────────────────

@register_tool
def reschedule_appointment(input: RescheduleAppointmentInput) -> RescheduleResult:
    """Reschedule an existing appointment to a new date and time."""
    client_id = input.account_id

    # ── Policy checks ────────────────────────────────────────────────────────
    appt = _get_appointment(client_id)
    if isinstance(appt, ToolError):
        return RescheduleResult(error=appt)

    if appt.status not in ("scheduled",):
        if appt.status == "none":
            return RescheduleResult(error=ToolError(
                code="NO_ACTIVE_APPOINTMENT",
                message="No active appointment found. Book a new appointment instead.",
            ))
        if appt.status == "pending_reschedule":
            return RescheduleResult(error=ToolError(
                code="PENDING_RESCHEDULE_ACTIVE",
                message="A reschedule is already in progress. Please wait for it to complete.",
            ))
        return RescheduleResult(error=ToolError(
            code="NO_ACTIVE_APPOINTMENT",
            message=f"Cannot reschedule — appointment status is '{appt.status}'.",
        ))

    if appt.has_pending_action:
        return RescheduleResult(error=ToolError(
            code="PENDING_RESCHEDULE_ACTIVE",
            message="There is a pending action on this account. Please resolve it before rescheduling.",
        ))

    old_date = appt.appointment_date

    try:
        new_date_obj = date.fromisoformat(input.new_date)
    except ValueError:
        return RescheduleResult(error=ToolError(
            code="INVALID_DATE",
            message=f"Could not parse date '{input.new_date}'. Use YYYY-MM-DD format.",
        ))

    today = date.today()
    if new_date_obj < today:
        return RescheduleResult(error=ToolError(
            code="DATE_IN_PAST",
            message="The new date is in the past. Please choose a future date.",
        ))

    if new_date_obj.weekday() == CLOSED_WEEKDAY:
        return RescheduleResult(error=ToolError(
            code="CLOSED_DAY",
            message="We are closed on Sundays. Please choose a Monday–Saturday date.",
        ))

    if not _is_available_on(new_date_obj):
        nearest = _nearest_outside_range(new_date_obj, new_date_obj)
        alts = [{"date": d.isoformat(), "day": d.strftime("%A")} for d in nearest]
        alt_str = " or ".join(f"{a['day']} {a['date']}" for a in alts) if alts else "another date"
        return RescheduleResult(
            alternatives=alts or None,
            error=ToolError(
                code="DATE_NOT_AVAILABLE",
                message=(
                    f"{new_date_obj.strftime('%A, %d %B')} is not available. "
                    f"Nearest available: {alt_str}."
                ),
            ),
        )

    # Same-day rescheduling incurs a fee
    fee = 0.0
    if old_date and old_date <= date.today():
        fee = RESCHEDULE_FEE

    svc_name = SERVICES.get(appt.service_type or "", {}).get("name", "appointment")
    old_date_str = old_date.strftime("%A, %d %B") if old_date else "current appointment"
    new_date_str = new_date_obj.strftime("%A, %d %B")
    fee_text = f"€{fee:.0f} same-day rescheduling fee" if fee > 0 else "No fee"

    # ── Return confirmation request on first call ────────────────────────────
    if not input.confirmed:
        payload = AppointmentConfirmationPayload(
            action="reschedule_appointment",
            summary=f"Move {svc_name} from {old_date_str} to {new_date_str}.",
            fee_impact=fee_text,
            old_date=old_date_str,
            new_date=new_date_str,
            raw={},
        )
        return RescheduleResult(
            confirmation_required=True,
            confirmation_payload=payload,
            old_date=old_date.isoformat() if old_date else None,
            new_date=new_date_obj.isoformat(),
            reschedule_fee=fee,
        )

    # ── Execute on confirmed=True ────────────────────────────────────────────
    new_state = AppointmentState(
        client_id=client_id,
        client_name=appt.client_name,
        service_type=appt.service_type,
        appointment_date=new_date_obj,
        status="scheduled",
        has_pending_action=False,
    )
    _persist_appointment(client_id, new_state)

    return RescheduleResult(
        success=True,
        old_date=old_date.isoformat() if old_date else None,
        new_date=new_date_obj.isoformat(),
        reschedule_fee=fee,
    )


# ─── Tool 5: Cancel appointment ──────────────────────────────────────────────

@register_tool
def cancel_appointment(input: CancelAppointmentInput) -> CancelResult:
    """Cancel a client's appointment. A late cancellation fee applies if under 24h notice."""
    client_id = input.account_id

    # ── Policy checks ────────────────────────────────────────────────────────
    appt = _get_appointment(client_id)
    if isinstance(appt, ToolError):
        return CancelResult(error=appt)

    if appt.status == "cancelled":
        return CancelResult(error=ToolError(
            code="ALREADY_CANCELLED_APPT",
            message="This appointment has already been cancelled.",
        ))

    if appt.status in ("none", "completed"):
        return CancelResult(error=ToolError(
            code="NO_ACTIVE_APPOINTMENT",
            message="No active appointment to cancel.",
        ))

    if appt.has_pending_action and not input.confirmed:
        return CancelResult(error=ToolError(
            code="PENDING_RESCHEDULE_ACTIVE",
            message=(
                "There is a pending reschedule on this account. "
                "Resolve or cancel the reschedule before cancelling the appointment."
            ),
        ))

    appt_date = appt.appointment_date
    svc_name = SERVICES.get(appt.service_type or "", {}).get("name", "appointment")

    # Same-day cancellation incurs a fee
    fee = 0.0
    fee_waived = True
    if appt_date and appt_date <= date.today():
        fee = CANCELLATION_FEE
        fee_waived = False

    appt_date_str = appt_date.strftime("%A, %d %B") if appt_date else "scheduled appointment"
    fee_text = (
        f"€{fee:.0f} same-day cancellation fee"
        if fee > 0
        else "No fee"
    )

    # ── Return confirmation request on first call ────────────────────────────
    if not input.confirmed:
        payload = AppointmentConfirmationPayload(
            action="cancel_appointment",
            summary=f"Cancel {svc_name} on {appt_date_str}.",
            fee_impact=fee_text,
            appointment_date=appt_date_str,
            service=svc_name,
            raw={},
        )
        return CancelResult(
            confirmation_required=True,
            confirmation_payload=payload,
            appointment_date=appt_date.isoformat() if appt_date else None,
            cancellation_fee=fee,
            fee_waived=fee_waived,
        )

    # ── Execute on confirmed=True ────────────────────────────────────────────
    cancelled_state = AppointmentState(
        client_id=client_id,
        client_name=appt.client_name,
        service_type=appt.service_type,
        appointment_date=appt_date,
        status="cancelled",
        has_pending_action=False,
    )
    _persist_appointment(client_id, cancelled_state)

    return CancelResult(
        success=True,
        appointment_date=appt_date.isoformat() if appt_date else None,
        cancellation_fee=fee,
        fee_waived=fee_waived,
    )
