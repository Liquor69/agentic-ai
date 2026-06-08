"""
domains/it_helpdesk.py

IT Helpdesk domain pack — second domain demonstrating extensibility.

Handles employee IT support requests: ticket lookups, password resets,
hardware requests, and ticket escalations.

To activate:  DOMAIN_PACK=it_helpdesk in .env
Tools module: tools/it_helpdesk.py

This is a proof-of-concept domain. It shows that adding a new business
domain requires only:
  1. This file (domain pack declaration)
  2. tools/it_helpdesk.py (tool implementations)
  No changes to core/, api/, or any other file.
"""

from __future__ import annotations

from domains.base import DomainPack

PACK: DomainPack = {
    "name": "it_helpdesk",
    "display_name": "IT Helpdesk",
    "description": (
        "Employee IT support: ticket status, password resets, "
        "hardware requests, and escalations."
    ),
    "tools_modules": [
        "tools.it_helpdesk",
    ],
    "classification_context": (
        "Employee IT support desk. "
        "Employees may check ticket status, request password resets, "
        "report hardware issues, or ask to escalate an open ticket."
    ),
    "selection_rules": """\
Tool selection rules:
- Employee asks about ticket status, updates, or progress          → check_ticket_status
- Employee needs a password reset or account unlock               → reset_password
- Employee reports a hardware issue or requests equipment         → hardware_request
- Employee asks to escalate or re-prioritise an existing ticket   → escalate_ticket
- Entire request is GENUINELY UNCLEAR                             → clarify (solo)

Disambiguation rules:
- "my laptop won't start" / "monitor is broken" → hardware_request, not escalate_ticket
- "still not fixed" / "urgent" / "need this today" → escalate_ticket
- "can't log in" / "forgot password" → reset_password
- "ticket 1234" / "my open request" / "how's my ticket" → check_ticket_status

Multi-tool examples:
- "Reset my password and check ticket #4521" → [reset_password, check_ticket_status]
- Single clear intent → one-element tools array

Extractable parameters (extract only values explicitly stated — never invent):
- check_ticket_status: ticket_id (string, e.g. "INC-4521")
- reset_password:      username (string, e.g. "jsmith")
- hardware_request:    description (free text describing the issue or item needed)
- escalate_ticket:     ticket_id (string), reason (brief justification text)\
""",
}
