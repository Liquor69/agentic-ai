"""
domains/appointments.py

Appointment & scheduling domain pack.

Handles client appointment requests: booking, rescheduling, cancellation,
availability checks, and policy/service FAQ.

To activate:  DOMAIN_PACK=appointments in .env
Tools module: tools/appointments.py

Adding this domain required only this file + tools/appointments.py.
Zero changes to core/, api/, or any existing file.
"""

from __future__ import annotations

from domains.base import DomainPack

PACK: DomainPack = {
    "name": "appointments",
    "display_name": "Appointment Assistant",
    "description": (
        "Client appointment booking, rescheduling, cancellation, "
        "availability checks, and service/policy Q&A."
    ),
    "tools_modules": [
        "tools.appointments",
    ],
    "classification_context": (
        "Service business appointment desk. "
        "Clients may book new appointments, reschedule or cancel existing ones, "
        "check availability, or ask about services and policies. "
        "IMPORTANT: Appointments are full-day bookings — there are no time slots. "
        "Never mention or suggest times (9:00, 11:00, 14:00, etc.)."
    ),
    "selection_rules": """\
Tool selection rules:
- Client asks to book, schedule, or make a new appointment       → book_appointment
- Client asks to reschedule, move, or change their appointment   → reschedule_appointment
- Client asks to cancel their appointment                        → cancel_appointment
- Client asks what times or slots are available                  → check_availability
- Client asks about services, prices, policies, or opening hours → appointment_faq
- Entire request is GENUINELY UNCLEAR                           → clarify (solo)

Disambiguation rules:
- "Can I move/change/shift my appointment" → reschedule_appointment, not cancel_appointment
- "What's your cancellation policy" → appointment_faq, not cancel_appointment
- "Is Tuesday free" / "Do you have openings" → check_availability
- "I need to cancel" / "Cancel my booking" → cancel_appointment
- "Book me in" / "I'd like to make an appointment" → book_appointment

Multi-tool examples:
- "Cancel my Thursday and book me for next Monday" → [cancel_appointment, book_appointment]
- Single clear intent → one-element tools array

Extractable parameters (extract only values explicitly stated — never invent):
- book_appointment:        requested_date (YYYY-MM-DD)
- reschedule_appointment:  new_date (YYYY-MM-DD)
- cancel_appointment:      (no extractable params — client_id comes from account context)
- check_availability:      date_range_start (YYYY-MM-DD), date_range_end (YYYY-MM-DD)
- appointment_faq:         question (exact question text)

Date range derivation for check_availability (today is always provided in the account context):
- Single day ("next Monday", "Thursday")     → date_range_start = that date, date_range_end = same date
- "next week"                                → date_range_start = next Monday, date_range_end = next Saturday
- "the week after next"                      → date_range_start = Monday in 2 weeks, date_range_end = Saturday in 2 weeks
- "this week"                                → date_range_start = today, date_range_end = this Saturday
- "next month"                               → date_range_start = 1st of next month, date_range_end = last day of next month
- No date or time mentioned                  → omit both fields entirely\
""",
}
