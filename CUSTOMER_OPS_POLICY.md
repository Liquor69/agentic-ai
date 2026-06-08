# Fitness World — Policy (Non-FAQ supplement)

## Cross-tool blocking rules
- Any active pending operation blocks ALL new action requests.
- Post-refund cancellation: blocked — no active subscription after refund.
- Post-cancellation refund: allowed if within 14-day window.
- Partial refund: never available under any condition.
- Plan change while paused: blocked.
- Pause extension mid-pause: not permitted.

## Confirmation requirement
Tools 4–7 (refund, cancel, pause, plan change) require explicit member confirmation before execution.
Confirmation payload must state: action summary, billing impact, member/guest pass impact,
subscription validity impact.
A new inbound message while confirmation is pending cancels the pending action and restarts from scratch.
Pending TTL: 10 minutes.

## Refund — full policy
Eligibility: purchase date ≤ 14 days since last purchase/renewal. No usage condition.
One refund per account lifetime.
Amount: subscription plan price only — extra fees (pause fees, replacement card) are non-refundable.
Annual plan: full €600 / €1,200 returned.
Effects: subscription invalidated immediately; guest pass invalidated immediately (Black Card).
Refund method: electronic payment → billing card; cash payment → cash.
Pending operation → refund blocked until resolved.

## Cancel — full policy
Disables auto-renewal. Subscription remains valid until prepaid period ends.
Guest pass valid until prepaid period ends (Black Card).
Reversible before period end (auto-renewal can be re-enabled).
Cannot cancel if there is no active subscription (e.g. post-refund).

## Pause — full policy
Applies to all plan types. ≥14 days notice. Duration pre-defined, cannot be extended.
1–3 months: €10/month fee. 4–6 months: escalate. 7+: deny.
Fee waiver (medical / military / job displacement): escalated for document validation.
  Staff validate documents; pause applied once verified. Not self-serve.
Billing cycle delayed by pause duration. All features suspended.

## Plan change — full policy
Upgrade (Pass → Black Card): delta billing, effective immediately. Guest pass issued at upgrade.
Downgrade (Black Card → Pass): no charge, scheduled at period end. Guest pass valid until then.
Billing-type change within same plan: treated as upgrade (higher cost) or downgrade (lower cost).
No cooldown between plan changes.
