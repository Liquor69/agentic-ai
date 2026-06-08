"""
domains/base.py

DomainPack TypedDict — the contract every domain must satisfy.

Adding a new domain requires exactly three steps:
  1. Create domains/<name>.py returning a DomainPack dict.
  2. Create tools/<name>.py with @register_tool functions.
  3. Set DOMAIN_PACK=<name> in .env.

Zero changes to core/, api/, or any existing file are required.
The planner reads selection_rules from the active domain pack at startup,
and tool discovery imports only that domain's tools_modules.
"""

from __future__ import annotations

from typing import TypedDict


class DomainPack(TypedDict):
    """
    Complete, self-contained configuration for one business domain.

    Fields
    ------
    name
        Machine identifier — must match the Python module name under domains/
        and the DOMAIN_PACK config value (e.g. "customer_ops").
    display_name
        Human-readable label shown in logs and /dashboard (e.g. "Customer Operations").
    description
        One-line summary of what this domain handles.
    tools_modules
        Python module paths to import during tool discovery so their @register_tool
        functions self-register into the registry.
        Example: ["tools.customer_ops", "tools.webhooks", "tools.external_api"]
    selection_rules
        Domain-specific routing instructions injected into the planner's tool-selection
        system prompt. Should list: tool selection rules, disambiguation rules,
        multi-tool examples, and extractable parameter names.
    classification_context
        One sentence injected at the start of the classification prompt to give the LLM
        domain context (e.g. "Gym membership customer support.").
    """
    name: str
    display_name: str
    description: str
    tools_modules: list[str]
    selection_rules: str
    classification_context: str
