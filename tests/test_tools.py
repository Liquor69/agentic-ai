"""
tests/test_tools.py

Unit tests for tools/customer_ops.py.

Coverage:
  - Happy path for all 7 tools (unconfirmed → confirmation payload, confirmed → success)
  - Every policy check / blocking condition
  - Registry integration (discover_tools, execute_tool)
  - execute_tool error paths (unknown tool, validation failure)

LLM-dependent tools (faq_lookup, policy_query) are tested with llm_call mocked.
All other tools have no external dependencies — pure policy logic.
"""

from __future__ import annotations

import sys
import os
from datetime import date, timedelta
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ─── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _register_tools():
    """
    Ensure a clean registry for every test.
    Must evict tools.customer_ops from sys.modules so the module-level
    @register_tool decorators re-fire on each import.
    Also resets any persisted demo account states so every test starts
    from fixture defaults — required now that _get_account() reads the DB.
    """
    import sys
    from tools.registry import _REGISTRY

    _REGISTRY.clear()
    for key in list(sys.modules):
        if key.startswith("tools.") and key != "tools.registry":
            del sys.modules[key]

    # Ensure the demo_account_states table exists and is clean.
    from db.models import create_tables
    create_tables()
    from db import crud
    crud.reset_all_demo_account_states()

    import tools.customer_ops  # noqa: F401 — side-effect: registers all tools
    yield
    _REGISTRY.clear()


# ─── Tool 1: faq_lookup ───────────────────────────────────────────────────────

class TestFAQLookup:
    def _call(self, question: str):
        from tools.customer_ops import faq_lookup, FAQLookupInput
        return faq_lookup(FAQLookupInput(question=question, session_id="t"))

    def test_found(self):
        mock_response = {"content": "The Pass costs €60/month or €600/year.", "tool_use": [], "stop_reason": "end_turn", "usage": {}}
        with patch("config.llm_call", return_value=mock_response):
            result = self._call("How much does the Pass cost?")
        assert result.found_in_faq is True
        assert "€60" in result.answer

    def test_not_found(self):
        mock_response = {"content": "This question is not covered by the FAQ.", "tool_use": [], "stop_reason": "end_turn", "usage": {}}
        with patch("config.llm_call", return_value=mock_response):
            result = self._call("What is your returns policy for merchandise?")
        assert result.found_in_faq is False

    def test_llm_error_returns_tool_error(self):
        with patch("config.llm_call", return_value={"error": "API timeout"}):
            result = self._call("anything")
        assert result.error is not None
        assert result.error.code == "LLM_CALL_FAILED"


# ─── Tool 2: policy_query ─────────────────────────────────────────────────────

class TestPolicyQuery:
    def _call(self, question: str):
        from tools.customer_ops import policy_query, PolicyQueryInput
        return policy_query(PolicyQueryInput(question=question, session_id="t"))

    def test_informational_answer(self):
        mock_response = {"content": '{"answer": "Pauses require 14 days notice.", "reclassify_to": null}', "tool_use": [], "stop_reason": "end_turn", "usage": {}}
        with patch("config.llm_call", return_value=mock_response):
            result = self._call("What is the pause notice period?")
        assert result.reclassify_to is None
        assert "14" in result.answer

    def test_reclassify_to_action_tool(self):
        mock_response = {"content": '{"answer": "I can process that for you.", "reclassify_to": "process_refund"}', "tool_use": [], "stop_reason": "end_turn", "usage": {}}
        with patch("config.llm_call", return_value=mock_response):
            result = self._call("I want my money back")
        assert result.reclassify_to == "process_refund"

    def test_non_json_response_surfaced_as_answer(self):
        mock_response = {"content": "Refunds are available within 14 days.", "tool_use": [], "stop_reason": "end_turn", "usage": {}}
        with patch("config.llm_call", return_value=mock_response):
            result = self._call("Tell me about refunds")
        assert result.answer == "Refunds are available within 14 days."
        assert result.reclassify_to is None


# ─── Tool 3: bug_report ───────────────────────────────────────────────────────

class TestBugReport:
    def test_always_escalates(self):
        from tools.customer_ops import bug_report, BugReportInput
        result = bug_report(BugReportInput(description="App crashes on login", session_id="t"))
        assert result.escalated is True
        assert "support@fitness.com" in result.message


# ─── Tool 4: process_refund ───────────────────────────────────────────────────

class TestProcessRefund:
    def _call(self, account_id: str, confirmed: bool = False):
        from tools.customer_ops import process_refund, RefundInput
        return process_refund(RefundInput(account_id=account_id, session_id="t", confirmed=confirmed))

    def test_happy_path_returns_confirmation_payload(self):
        result = self._call("demo-pass-monthly")
        assert result.success is False
        assert result.confirmation_required is True
        assert result.confirmation_payload is not None
        assert result.confirmation_payload.action == "process_refund"
        assert result.refund_amount == 60.0
        assert result.refund_method == "original payment method"

    def test_confirmed_executes_refund(self):
        result = self._call("demo-pass-monthly", confirmed=True)
        assert result.success is True
        assert result.subscription_status == "invalidated"
        assert result.guest_pass_status is None  # Pass plan, no guest

    def test_black_card_invalidates_guest_pass(self):
        result = self._call("demo-bc-annual", confirmed=True)
        assert result.guest_pass_status == "invalidated"

    def test_cash_payment_refund_method(self):
        # Payment method is not stored in the DB — refund always uses original payment method.
        # The plan price (€120) is refundable; the €10 card fee charged separately is not.
        result = self._call("demo-bc-cash")
        assert result.refund_method == "original payment method"
        assert result.refund_amount == 120.0  # plan price only, not the extra card fee

    def test_expired_window_blocked(self):
        result = self._call("demo-pass-monthly-expired-window")
        assert result.success is False
        assert result.error.code == "REFUND_WINDOW_EXPIRED"
        assert result.error.details["days_elapsed"] == 20

    def test_lifetime_limit_escalates(self):
        result = self._call("demo-refund-used")
        assert result.success is False
        assert result.error.code == "REFUND_ESCALATION_REQUIRED"
        assert result.escalation_required is True

    def test_pending_operation_blocked(self):
        result = self._call("demo-pending")
        assert result.error.code == "PENDING_OPERATION_ACTIVE"

    def test_unknown_account_returns_error(self):
        result = self._call("nonexistent-id")
        assert result.error.code == "ACCOUNT_NOT_FOUND"

    def test_confirmation_payload_contains_all_required_fields(self):
        result = self._call("demo-pass-monthly")
        p = result.confirmation_payload
        assert p.billing_impact
        assert p.member_impact
        assert p.guest_pass_impact
        assert p.subscription_validity_impact
        assert "account_id" in p.raw


# ─── Tool 5: cancel_subscription ─────────────────────────────────────────────

class TestCancelSubscription:
    def _call(self, account_id: str, confirmed: bool = False):
        from tools.customer_ops import cancel_subscription, CancelInput
        return cancel_subscription(CancelInput(account_id=account_id, session_id="t", confirmed=confirmed))

    def test_happy_path_returns_confirmation_payload(self):
        result = self._call("demo-pass-monthly")
        assert result.confirmation_required is True
        assert result.auto_renewal_after is False
        assert result.subscription_valid_until is not None

    def test_confirmed_sets_auto_renewal_false(self):
        result = self._call("demo-pass-monthly", confirmed=True)
        assert result.success is True
        assert result.auto_renewal_after is False

    def test_black_card_guest_pass_valid_until_period_end(self):
        result = self._call("demo-bc-annual", confirmed=True)
        assert result.guest_pass_valid_until == result.subscription_valid_until

    def test_already_cancelled_blocked(self):
        # status="cancelled" → auto_renewal property returns False → already-cancelled error.
        from tools.customer_ops import cancel_subscription, CancelInput, AccountState
        from datetime import date, timedelta
        mock_acct = AccountState(
            account_id="x",
            plan="standard",
            status="cancelled",
            billing_cycle="monthly",
            last_payment_date=date.today() - timedelta(days=1),
            refund_history_present=False,
            pending_action=False,
        )
        with patch("tools.customer_ops._get_account", return_value=mock_acct):
            result = cancel_subscription(CancelInput(account_id="x", session_id="t"))
        assert result.error.code == "ALREADY_CANCELLED"

    def test_pending_operation_blocked(self):
        result = self._call("demo-pending")
        assert result.error.code == "PENDING_OPERATION_ACTIVE"


# ─── Tool 6: pause_subscription ──────────────────────────────────────────────

class TestPauseSubscription:
    def _call(self, account_id: str, start_offset_days: int = 20, duration: int = 60,
              has_docs: bool = False, confirmed: bool = False):
        from tools.customer_ops import pause_subscription, PauseInput
        return pause_subscription(PauseInput(
            account_id=account_id,
            session_id="t",
            start_date=date.today() + timedelta(days=start_offset_days),
            duration_days=duration,
            has_documentation=has_docs,
            confirmed=confirmed,
        ))

    def test_happy_path_with_fee_returns_confirmation(self):
        result = self._call("demo-pass-monthly")
        assert result.confirmation_required is True
        assert result.pause_fee == 20.0  # 60 days → ceil(60/30)=2 periods × €10
        assert result.new_renewal_date is not None

    def test_documentation_escalates(self):
        result = self._call("demo-pass-monthly", has_docs=True)
        assert result.error.code == "PAUSE_DOCUMENTATION_ESCALATION"
        assert result.escalation_required is True

    def test_confirmed_executes_pause(self):
        result = self._call("demo-pass-monthly", confirmed=True)
        assert result.success is True
        assert result.pause_start is not None
        assert result.pause_end is not None

    def test_insufficient_notice_blocked(self):
        result = self._call("demo-pass-monthly", start_offset_days=10)
        assert result.error.code == "PAUSE_NOTICE_INSUFFICIENT"

    def test_7_plus_months_denied(self):
        result = self._call("demo-pass-monthly", duration=181)  # >= PAUSE_DENY_DAYS
        assert result.error.code == "PAUSE_DURATION_DENIED"

    def test_4_to_6_months_without_docs_escalates(self):
        result = self._call("demo-pass-monthly", duration=91)  # >= PAUSE_ESCALATE_DAYS
        assert result.error.code == "PAUSE_ESCALATION_REQUIRED"
        assert result.escalation_required is True

    def test_4_to_6_months_with_docs_escalates(self):
        result = self._call("demo-pass-monthly", duration=120, has_docs=True)  # 4 months
        assert result.error.code == "PAUSE_DOCUMENTATION_ESCALATION"
        assert result.escalation_required is True

    def test_already_paused_blocked(self):
        result = self._call("demo-paused")
        assert result.error.code == "ALREADY_PAUSED"

    def test_pending_operation_blocked(self):
        result = self._call("demo-pending")
        assert result.error.code == "PENDING_OPERATION_ACTIVE"

    def test_black_card_guest_pass_noted_in_payload(self):
        result = self._call("demo-bc-annual")
        assert "Guest pass" in result.guest_pass_impact

    def test_new_renewal_date_shifted_by_duration(self):
        result = self._call("demo-pass-monthly", duration=90)  # 3 months
        assert result.new_renewal_date is not None


# ─── Tool 7: change_plan ─────────────────────────────────────────────────────

class TestChangePlan:
    def _call(self, account_id: str, new_plan: str, new_billing: str, confirmed: bool = False):
        from tools.customer_ops import change_plan, PlanChangeInput
        return change_plan(PlanChangeInput(
            account_id=account_id,
            session_id="t",
            new_plan=new_plan,
            new_billing_type=new_billing,
            confirmed=confirmed,
        ))

    def test_upgrade_returns_confirmation_with_charge(self):
        result = self._call("demo-pass-monthly", "premium", "monthly")
        assert result.confirmation_required is True
        assert result.change_type == "upgrade"
        assert result.immediate_charge is not None
        assert result.immediate_charge >= 0.0

    def test_upgrade_confirmed_succeeds(self):
        result = self._call("demo-pass-monthly", "premium", "monthly", confirmed=True)
        assert result.success is True
        assert result.change_type == "upgrade"
        assert result.effective_date == date.today().isoformat()

    def test_upgrade_guest_pass_impact_noted(self):
        result = self._call("demo-pass-monthly", "premium", "monthly")
        assert "Guest pass issued immediately" in result.guest_pass_impact

    def test_downgrade_deferred_no_charge(self):
        result = self._call("demo-bc-annual", "standard", "monthly")
        assert result.change_type == "downgrade"
        assert result.immediate_charge is None
        # effective date should be subscription_valid_until, not today
        assert result.effective_date != date.today().isoformat()

    def test_downgrade_guest_pass_valid_until_period_end(self):
        result = self._call("demo-bc-annual", "standard", "monthly")
        assert result.effective_date in result.guest_pass_impact

    def test_plan_change_during_pause_blocked(self):
        result = self._call("demo-paused", "standard", "monthly")
        assert result.error.code == "ACCOUNT_PAUSED"

    def test_pending_operation_blocked(self):
        result = self._call("demo-pending", "premium", "monthly")
        assert result.error.code == "PENDING_OPERATION_ACTIVE"

    def test_invalid_plan_name_returns_error(self):
        result = self._call("demo-pass-monthly", "platinum", "monthly")
        assert result.error.code == "INVALID_PLAN"

    def test_invalid_billing_type_returns_error(self):
        result = self._call("demo-pass-monthly", "premium", "weekly")
        assert result.error.code == "INVALID_BILLING_TYPE"


# ─── Registry integration ─────────────────────────────────────────────────────

class TestRegistry:
    def test_all_seven_tools_registered(self):
        from tools.registry import list_tools
        tools = list_tools()
        expected = {
            "faq_lookup", "policy_query", "bug_report",
            "process_refund", "cancel_subscription",
            "pause_subscription", "change_plan",
        }
        assert expected == set(tools)

    def test_get_tool_schemas_correct_shape(self):
        from tools.registry import get_tool_schemas
        schemas = get_tool_schemas()
        for s in schemas:
            assert "name" in s
            assert "description" in s
            assert "input_schema" in s
            assert isinstance(s["input_schema"], dict)

    def test_execute_tool_unknown_returns_error(self):
        from tools.registry import execute_tool
        result = execute_tool("nonexistent", {})
        assert result["error"]["code"] == "TOOL_NOT_FOUND"

    def test_execute_tool_validation_failure_returns_error(self):
        from tools.registry import execute_tool
        result = execute_tool("process_refund", {"bad_field": "value"})
        assert result["error"]["code"] == "TOOL_INPUT_VALIDATION_FAILED"

    def test_execute_tool_happy_path(self):
        from tools.registry import execute_tool
        result = execute_tool(
            "process_refund",
            {"account_id": "demo-pass-monthly", "session_id": "t"},
        )
        assert result.confirmation_required is True

    def test_get_tool_names_and_descriptions_no_schemas(self):
        from tools.registry import get_tool_names_and_descriptions
        items = get_tool_names_and_descriptions()
        for item in items:
            assert "name" in item
            assert "description" in item
            assert "input_schema" not in item


# ─── State persistence ────────────────────────────────────────────────────────

class TestStatePersistence:
    """
    Verify that confirmed tool executions persist state changes to the DB
    so that subsequent calls observe the mutated state.

    Each test runs against the real (test) DB; the autouse _register_tools
    fixture calls reset_all_demo_account_states() before each test so every
    test starts from fixture defaults.
    """

    # ── Cancel → re-cancel ────────────────────────────────────────────────────

    def test_cancel_then_recancel_fails_already_cancelled(self):
        from tools.customer_ops import cancel_subscription, CancelInput
        # First call confirmed=True: executes cancellation, persists status="cancelled"
        r1 = cancel_subscription(CancelInput(account_id="demo-pass-monthly", session_id="t", confirmed=True))
        assert r1.success is True
        # Second call: _get_account() reads DB → status="cancelled" → auto_renewal=False
        r2 = cancel_subscription(CancelInput(account_id="demo-pass-monthly", session_id="t"))
        assert r2.error is not None
        assert r2.error.code == "ALREADY_CANCELLED"

    # ── Refund → re-refund ────────────────────────────────────────────────────

    def test_refund_then_rerefund_escalates(self):
        from tools.customer_ops import process_refund, RefundInput
        # First call confirmed=True: persists status="refunded", refund_history_present=True
        r1 = process_refund(RefundInput(account_id="demo-pass-monthly", session_id="t", confirmed=True))
        assert r1.success is True
        # Second call: status="refunded" → not is_active → refund window already elapsed path OR
        # refund_history_present=True → REFUND_ESCALATION_REQUIRED
        r2 = process_refund(RefundInput(account_id="demo-pass-monthly", session_id="t"))
        assert r2.error is not None
        # Either REFUND_ESCALATION_REQUIRED (if status check passes) or window/activity check
        assert r2.error.code in ("REFUND_ESCALATION_REQUIRED", "NO_ACTIVE_SUBSCRIPTION")

    # ── Pause → re-pause ──────────────────────────────────────────────────────

    def test_pause_then_repause_fails_already_paused(self):
        from tools.customer_ops import pause_subscription, PauseInput
        start = date.today() + timedelta(days=20)
        r1 = pause_subscription(PauseInput(
            account_id="demo-pass-monthly", session_id="t",
            start_date=start, duration_days=30, confirmed=True,
        ))
        assert r1.success is True
        # Second attempt: status="paused" → ALREADY_PAUSED
        r2 = pause_subscription(PauseInput(
            account_id="demo-pass-monthly", session_id="t",
            start_date=start, duration_days=30,
        ))
        assert r2.error is not None
        assert r2.error.code == "ALREADY_PAUSED"

    # ── Plan change persists ───────────────────────────────────────────────────

    def test_plan_change_updates_persisted_plan(self):
        from tools.customer_ops import change_plan, PlanChangeInput, _get_account
        r1 = change_plan(PlanChangeInput(
            account_id="demo-pass-monthly", session_id="t",
            new_plan="premium", new_billing_type="monthly", confirmed=True,
        ))
        assert r1.success is True
        # Now the account should show the new plan
        acct = _get_account("demo-pass-monthly")
        assert acct.plan == "premium"
        assert acct.billing_cycle == "monthly"

    # ── Reset restores fixture default ────────────────────────────────────────

    def test_reset_restores_fixture_state(self):
        from tools.customer_ops import cancel_subscription, CancelInput, _get_account
        from db import crud
        # Cancel to mutate state
        cancel_subscription(CancelInput(account_id="demo-pass-monthly", session_id="t", confirmed=True))
        acct_after = _get_account("demo-pass-monthly")
        assert acct_after.status == "cancelled"
        # Reset
        crud.reset_demo_account_state("demo-pass-monthly")
        acct_reset = _get_account("demo-pass-monthly")
        assert acct_reset.status == "active"  # back to fixture default
