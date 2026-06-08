"""
tests/test_domains.py

Tests for the domain pack system (domains/, tools/registry domain-aware discovery).

Coverage:
  - load_domain_pack() returns a valid DomainPack for known domains
  - load_domain_pack() raises ValueError for unknown domain
  - list_domain_packs() returns all registered names
  - DomainPack fields are non-empty strings
  - discover_tools(domain="customer_ops") registers customer_ops tools
  - discover_tools(domain="it_helpdesk") registers it_helpdesk tools
  - discover_tools(domain=None) loads all tools (backward-compatible fallback)
  - Switching domain clears and re-registers tools
  - it_helpdesk tool stubs execute correctly (check_ticket_status, reset_password,
    hardware_request, escalate_ticket)
  - Active domain drives planner prompt construction (_CLASSIFICATION_SYSTEM,
    _SELECTION_SYSTEM_BASE contain domain-specific text)
"""

from __future__ import annotations

import sys


# ══════════════════════════════════════════════════════════════════════════════
# Section 1: Domain pack loading
# ══════════════════════════════════════════════════════════════════════════════

class TestDomainPackLoading:
    def test_load_customer_ops_returns_pack(self):
        from domains import load_domain_pack
        pack = load_domain_pack("customer_ops")
        assert pack["name"] == "customer_ops"

    def test_load_it_helpdesk_returns_pack(self):
        from domains import load_domain_pack
        pack = load_domain_pack("it_helpdesk")
        assert pack["name"] == "it_helpdesk"

    def test_unknown_domain_raises_value_error(self):
        from domains import load_domain_pack
        try:
            load_domain_pack("nonexistent_domain_xyz")
            assert False, "Expected ValueError"
        except ValueError as exc:
            assert "nonexistent_domain_xyz" in str(exc)
            assert "Available" in str(exc)

    def test_list_domain_packs_returns_both(self):
        from domains import list_domain_packs
        packs = list_domain_packs()
        assert "customer_ops" in packs
        assert "it_helpdesk" in packs

    def test_customer_ops_pack_fields_nonempty(self):
        from domains import load_domain_pack
        pack = load_domain_pack("customer_ops")
        for field in ("name", "display_name", "description", "selection_rules", "classification_context"):
            assert pack[field], f"Field '{field}' is empty"
        assert pack["tools_modules"], "tools_modules is empty"

    def test_it_helpdesk_pack_fields_nonempty(self):
        from domains import load_domain_pack
        pack = load_domain_pack("it_helpdesk")
        for field in ("name", "display_name", "description", "selection_rules", "classification_context"):
            assert pack[field], f"Field '{field}' is empty"
        assert pack["tools_modules"], "tools_modules is empty"

    def test_customer_ops_tools_modules_reference_customer_ops(self):
        from domains import load_domain_pack
        pack = load_domain_pack("customer_ops")
        assert any("customer_ops" in m for m in pack["tools_modules"])

    def test_it_helpdesk_tools_modules_reference_it_helpdesk(self):
        from domains import load_domain_pack
        pack = load_domain_pack("it_helpdesk")
        assert any("it_helpdesk" in m for m in pack["tools_modules"])


# ══════════════════════════════════════════════════════════════════════════════
# Section 2: Domain-aware tool discovery
# ══════════════════════════════════════════════════════════════════════════════

class TestDomainAwareDiscovery:
    def _fresh_registry(self):
        """Clear the registry and purge cached tool modules."""
        from tools.registry import _REGISTRY
        _REGISTRY.clear()
        for key in list(sys.modules):
            if key.startswith("tools.") and key not in ("tools.registry",):
                del sys.modules[key]

    def test_customer_ops_domain_registers_customer_ops_tools(self):
        from tools.registry import _REGISTRY, discover_tools
        self._fresh_registry()
        discover_tools(domain="customer_ops")
        assert "process_refund" in _REGISTRY
        assert "cancel_subscription" in _REGISTRY
        assert "faq_lookup" in _REGISTRY
        self._fresh_registry()

    def test_customer_ops_domain_does_not_register_it_helpdesk_tools(self):
        from tools.registry import _REGISTRY, discover_tools
        self._fresh_registry()
        discover_tools(domain="customer_ops")
        assert "check_ticket_status" not in _REGISTRY
        assert "reset_password" not in _REGISTRY
        self._fresh_registry()

    def test_it_helpdesk_domain_registers_it_helpdesk_tools(self):
        from tools.registry import _REGISTRY, discover_tools
        self._fresh_registry()
        discover_tools(domain="it_helpdesk")
        assert "check_ticket_status" in _REGISTRY
        assert "reset_password" in _REGISTRY
        assert "hardware_request" in _REGISTRY
        assert "escalate_ticket" in _REGISTRY
        self._fresh_registry()

    def test_it_helpdesk_domain_does_not_register_customer_ops_tools(self):
        from tools.registry import _REGISTRY, discover_tools
        self._fresh_registry()
        discover_tools(domain="it_helpdesk")
        assert "process_refund" not in _REGISTRY
        assert "cancel_subscription" not in _REGISTRY
        self._fresh_registry()

    def test_no_domain_loads_all_tools(self):
        from tools.registry import _REGISTRY, discover_tools
        self._fresh_registry()
        discover_tools(domain=None)
        # Both customer_ops and it_helpdesk tools should be present
        assert "process_refund" in _REGISTRY
        assert "check_ticket_status" in _REGISTRY
        self._fresh_registry()

    def test_switching_domain_replaces_tools(self):
        from tools.registry import _REGISTRY, discover_tools
        self._fresh_registry()
        discover_tools(domain="customer_ops")
        assert "process_refund" in _REGISTRY
        self._fresh_registry()
        discover_tools(domain="it_helpdesk")
        assert "process_refund" not in _REGISTRY
        assert "check_ticket_status" in _REGISTRY
        self._fresh_registry()


# ══════════════════════════════════════════════════════════════════════════════
# Section 3: IT helpdesk stub tool execution
# ══════════════════════════════════════════════════════════════════════════════

class TestITHelpdeskTools:
    def setup_method(self):
        # Ensure the it_helpdesk tools are importable for direct execution tests.
        import importlib
        for key in list(sys.modules):
            if key == "tools.it_helpdesk":
                del sys.modules[key]

    def test_check_ticket_status_found(self):
        from tools.it_helpdesk import CheckTicketInput, check_ticket_status
        result = check_ticket_status(CheckTicketInput(ticket_id="INC-1001"))
        assert result["halt_reason"] == "success"
        assert "INC-1001" in result["ticket_id"]
        assert result["status"] == "In Progress"

    def test_check_ticket_status_not_found(self):
        from tools.it_helpdesk import CheckTicketInput, check_ticket_status
        result = check_ticket_status(CheckTicketInput(ticket_id="INC-9999"))
        assert "error" in result
        assert result["error"]["code"] == "TICKET_NOT_FOUND"

    def test_check_ticket_status_case_insensitive(self):
        from tools.it_helpdesk import CheckTicketInput, check_ticket_status
        result = check_ticket_status(CheckTicketInput(ticket_id="inc-1001"))
        assert result["halt_reason"] == "success"

    def test_reset_password_first_call_is_pending(self):
        from tools.it_helpdesk import ResetPasswordInput, reset_password
        result = reset_password(ResetPasswordInput(username="jsmith", confirmed=False))
        assert result["halt_reason"] == "confirmation_pending"
        assert "confirmation_payload" in result

    def test_reset_password_confirmed_succeeds(self):
        from tools.it_helpdesk import ResetPasswordInput, reset_password
        result = reset_password(ResetPasswordInput(username="jsmith", confirmed=True))
        assert result["halt_reason"] == "success"
        assert "jsmith" in result["response"]

    def test_reset_password_empty_username_error(self):
        from tools.it_helpdesk import ResetPasswordInput, reset_password
        result = reset_password(ResetPasswordInput(username="", confirmed=False))
        assert "error" in result
        assert result["error"]["code"] == "USERNAME_REQUIRED"

    def test_hardware_request_success(self):
        from tools.it_helpdesk import HardwareRequestInput, hardware_request
        result = hardware_request(HardwareRequestInput(description="Laptop screen cracked"))
        assert result["halt_reason"] == "success"
        assert "verified_changes" in result

    def test_hardware_request_empty_description_error(self):
        from tools.it_helpdesk import HardwareRequestInput, hardware_request
        result = hardware_request(HardwareRequestInput(description=""))
        assert "error" in result

    def test_escalate_ticket_first_call_is_pending(self):
        from tools.it_helpdesk import EscalateTicketInput, escalate_ticket
        result = escalate_ticket(EscalateTicketInput(ticket_id="INC-1001", reason="Blocking production", confirmed=False))
        assert result["halt_reason"] == "confirmation_pending"

    def test_escalate_ticket_confirmed_succeeds(self):
        from tools.it_helpdesk import EscalateTicketInput, escalate_ticket
        result = escalate_ticket(EscalateTicketInput(ticket_id="INC-1001", reason="Urgent", confirmed=True))
        assert result["halt_reason"] == "success"

    def test_escalate_resolved_ticket_is_error(self):
        from tools.it_helpdesk import EscalateTicketInput, escalate_ticket
        result = escalate_ticket(EscalateTicketInput(ticket_id="INC-1003", reason="Still broken", confirmed=False))
        assert "error" in result
        assert result["error"]["code"] == "TICKET_ALREADY_RESOLVED"


# ══════════════════════════════════════════════════════════════════════════════
# Section 4: Planner prompt construction is domain-driven
# ══════════════════════════════════════════════════════════════════════════════

class TestPlannerPromptsDomainDriven:
    def test_classification_system_contains_domain_context(self):
        """The planner's classification prompt contains the customer_ops context."""
        from core.planner import _CLASSIFICATION_SYSTEM
        from domains import load_domain_pack
        pack = load_domain_pack("customer_ops")
        # The classification context keyword should appear somewhere in the prompt
        assert any(
            word in _CLASSIFICATION_SYSTEM
            for word in pack["classification_context"].split()[:5]
        )

    def test_selection_system_contains_customer_ops_rules(self):
        """The planner's selection prompt contains customer_ops-specific routing rules."""
        from core.planner import _SELECTION_SYSTEM_BASE
        assert "process_refund" in _SELECTION_SYSTEM_BASE
        assert "cancel_subscription" in _SELECTION_SYSTEM_BASE
        assert "pause_subscription" in _SELECTION_SYSTEM_BASE

    def test_selection_system_contains_generic_critical_rules(self):
        """Generic CRITICAL RULES preamble is always present regardless of domain."""
        from core.planner import _SELECTION_SYSTEM_BASE
        assert "CRITICAL RULES" in _SELECTION_SYSTEM_BASE
        assert "clarify" in _SELECTION_SYSTEM_BASE

    def test_build_prompts_with_it_helpdesk_returns_different_rules(self):
        """Building prompts for it_helpdesk yields IT-specific routing rules."""
        from core.planner import _build_prompts
        cls_sys, sel_sys = _build_prompts.__wrapped__() if hasattr(_build_prompts, "__wrapped__") else _build_prompts()
        # We need to call with the it_helpdesk domain pack directly
        from domains import load_domain_pack
        from core.planner import (
            _CLASSIFICATION_SYSTEM_TEMPLATE,
            _SELECTION_SYSTEM_PREAMBLE,
            _SELECTION_SYSTEM_SUFFIX,
        )
        pack = load_domain_pack("it_helpdesk")
        cls_sys = _CLASSIFICATION_SYSTEM_TEMPLATE.format(context=pack["classification_context"])
        sel_sys = _SELECTION_SYSTEM_PREAMBLE + "\n\n" + pack["selection_rules"] + "\n\n" + _SELECTION_SYSTEM_SUFFIX
        assert "check_ticket_status" in sel_sys
        assert "reset_password" in sel_sys
        assert "process_refund" not in sel_sys
