"""
domains/customer_ops.py

Customer Operations domain pack — the default domain.

Handles gym/fitness-club membership actions: refunds, cancellations,
pauses, plan changes, payment history, FAQ, and policy queries.

Tools:  tools/customer_ops.py, tools/webhooks.py, tools/external_api.py
Active: set DOMAIN_PACK=customer_ops in .env (this is the default)
"""

from __future__ import annotations

from domains.base import DomainPack

PACK: DomainPack = {
    "name": "customer_ops",
    "display_name": "Customer Operations",
    "description": (
        "Gym / fitness-club membership customer support: "
        "refunds, cancellations, pauses, plan changes, billing history, FAQ, and policy."
    ),
    "tools_modules": [
        "tools.customer_ops",
        "tools.webhooks",
        "tools.external_api",
    ],
    "classification_context": (
        "Gym membership customer support. "
        "Members may request refunds, cancellations, pauses, plan upgrades/downgrades, "
        "billing history, or ask factual or policy questions about the gym."
    ),
    "selection_rules": """\
Tool selection rules:
- Member requests a refund or wants money back                        → process_refund
- Member asks to cancel / not renew (permanent, no time limit)       → cancel_subscription
- Member uses pause / freeze / suspend / hold (any or no duration)   → pause_subscription
- Member asks to change plan or billing cycle                        → change_plan
- Member asks about past charges or billing history                  → fetch_payment_history
- Member asks factual question about gym (hours, pricing, plans)     → faq_lookup
- Member asks about eligibility, policy rules, or "can I..."         → policy_query
- Entire request is GENUINELY UNCLEAR (e.g. "fix my account", "help") → clarify (solo)

Disambiguation rules:
- "pause" / "freeze" / "hold" / "suspend" — even without a duration → pause_subscription
  (the form collects start date and duration; do NOT ask the member yourself)
- "cancel for a few months" / "stop it for a while" → pause_subscription, not cancel
- "cancel and get money back" / "I changed my mind" → process_refund
- "switch plans" / "downgrade" / "change to annual" → change_plan

Multi-tool examples:
- "What are your hours? Also I'd like to pause my subscription." → [faq_lookup, pause_subscription]
- "Can I get a refund? And what's my billing history?" → [process_refund, fetch_payment_history]
- Single clear intent → one-element tools array

Extractable parameters (extract only values explicitly stated — never invent):
- pause_subscription: start_date (YYYY-MM-DD), duration_days (integer, min 30), end_date (YYYY-MM-DD)
- change_plan:        new_plan ("standard" or "premium"), new_billing_type ("monthly" or "annual")
- faq_lookup:         question (exact member question text)
- policy_query:       question (exact member question text)\
""",
}
