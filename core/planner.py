"""
core/planner.py

LangGraph state machine:
  interpretation → selection → [form | execution] → log → respond

LLM calls: interpretation (Haiku, intent classification) and selection (Haiku, tool selection).
Everything downstream — validation, confirmation, execution — is hardcoded.

Graph topology:
  interpretation → [selection | execution | respond]
    - confirmed=True + pending found   → execution   (skip re-selection)
    - confirmed=True + pending absent  → respond     (TTL expired)
    - confirmed=False (cancelled)      → respond
    - normal first call                → selection

  selection → [execution | form | log]
    - tool selected, all required params present → execution
    - tool selected, required params missing     → form
    - ambiguity / max_iterations / LLM error    → log

  form    → log  (halt: form_required — stores pending, returns form spec to frontend)
  execution → log
  log       → respond
  respond   → END

Halting conditions:
  success              — tool executed, result returned
  tool_failure         — tool returned a ToolError or registry error
  max_iterations       — exceeded MAX_ITERATIONS planning cycles
  ambiguity            — LLM could not select a tool; clarifying question returned
  form_required        — required parameters missing; form returned for user to complete
  confirmation_pending  — action tool requires explicit member YES/NO before executing
  cancelled             — member chose NO on a pending confirmation
  confirmation_expired  — confirmed=True arrived but TTL had elapsed; resubmit required
"""

from __future__ import annotations

import json
import logging
from datetime import date
from functools import lru_cache
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from config import llm_call, settings
from core import memory
from core.logger import log_run
from db import crud
from tools.registry import execute_tool, get_tool_names_and_descriptions

logger = logging.getLogger(__name__)

MAX_ITERATIONS: int = settings.max_iterations


# ─── State ────────────────────────────────────────────────────────────────────

class AgentState(TypedDict):
    # ── Input ──────────────────────────────────────────────────────────────────
    input: str
    session_id: str
    allow_history_reference: bool
    confirmed: bool | None         # None = normal, True = YES, False = NO/cancel
    account_id: str | None
    account_context: str | None    # human-readable summary from describe_account()
    form_data: dict | None         # form field values submitted alongside confirmed=True
    dry_run: bool                  # True = simulate execution, no side effects
    domain: str | None             # active domain pack name (forwarded from RunRequest)

    # ── Interpretation ─────────────────────────────────────────────────────────
    classification: str | None     # e.g. "Subscription pause request — dates unspecified"

    # ── Selection ──────────────────────────────────────────────────────────────
    intent: str | None
    selected_tool: str | None
    available_tools: list[dict]    # registry snapshot at planning time
    selection_justification: str | None
    tool_input_data: dict | None
    form_spec: dict | None         # populated when halt_reason="form_required"

    # ── Confirmation ───────────────────────────────────────────────────────────
    confirmation_payload: dict | None
    confirmation_value: bool | None  # True=confirmed, False=cancelled, None=not applicable

    # ── Execution ──────────────────────────────────────────────────────────────
    selected_tools: list[dict] | None   # [{tool_name, tool_input, justification}, ...]
    tool_result: Any
    tool_results: list[dict]            # per-tool outcomes: [{tool_name, halt_reason, ...}, ...]
    verified_changes: list[str] | None

    # ── Control ────────────────────────────────────────────────────────────────
    error: dict | None
    iterations: int
    halt_reason: str | None
    clarifying_question: str | None

    # ── Session ────────────────────────────────────────────────────────────────
    history: list[dict]

    # ── Output ─────────────────────────────────────────────────────────────────
    final_response: str | None
    trace: list[dict]


# ─── Prompt constants ─────────────────────────────────────────────────────────
# Generic preamble and suffix are domain-agnostic.
# The domain-specific tool routing rules are injected from the active domain pack.

_CLASSIFICATION_SYSTEM_TEMPLATE = (
    "{context} "
    "Classify the user's request in one short phrase (5–12 words). "
    "Be specific: name the action and any key condition. "
    "Output the phrase only — no punctuation at the end, no explanation."
)

_SELECTION_SYSTEM_PREAMBLE = """\
You are an operations planning agent. Select the correct tool(s) based on the user's intent.

CRITICAL RULES:
1. Select based on INTENT only. Missing parameters are NOT a reason to use "clarify".
   The system will automatically prompt the user for any missing parameters via a form.
2. If the request covers multiple distinct intents, include ALL required tools in the tools array.
3. Only use "clarify" (as the sole entry) when the ENTIRE request is genuinely unclear.
4. NEVER add an FAQ or informational tool alongside a booking/action intent. Action tools
   handle incomplete information internally. Adding FAQ tools to "help" the user is wrong.\
"""

_SELECTION_SYSTEM_SUFFIX = """\
Call execute_plan with a tools array. Each entry has tool_name, tool_input (extractable params only),
and justification. Do NOT include account_id, session_id, or confirmed in any tool_input.\
"""


@lru_cache(maxsize=8)
def _get_prompts_for_domain(domain: str) -> tuple[str, str]:
    """
    Build (classification_system, selection_system_base) for a specific domain. Cached per domain.
    Using state['domain'] per request allows a single server to serve multiple domain tabs correctly.
    """
    from domains import load_domain_pack
    pack = load_domain_pack(domain)
    classification = _CLASSIFICATION_SYSTEM_TEMPLATE.format(
        context=pack["classification_context"]
    )
    selection = (
        _SELECTION_SYSTEM_PREAMBLE
        + "\n\n"
        + pack["selection_rules"]
        + "\n\n"
        + _SELECTION_SYSTEM_SUFFIX
    )
    return classification, selection


# Module-level constants for backward compat (test_domains.py imports these directly)
_CLASSIFICATION_SYSTEM, _SELECTION_SYSTEM_BASE = _get_prompts_for_domain(settings.domain_pack)

_EXECUTE_PLAN_SCHEMA: dict[str, Any] = {
    "name": "execute_plan",
    "description": "Select one or more tools to execute sequentially and document the reasoning.",
    "input_schema": {
        "type": "object",
        "properties": {
            "tools": {
                "type": "array",
                "description": (
                    "Ordered list of tools to execute. Include ALL tools needed to fully address "
                    "the request. Most requests need one tool; multi-intent requests may need several."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "tool_name": {
                            "type": "string",
                            "description": (
                                "Exact registered tool name. Use 'clarify' as the sole entry "
                                "only when the entire request is genuinely unclear."
                            ),
                        },
                        "tool_input": {
                            "type": "object",
                            "description": (
                                "Parameters extractable from the message only. "
                                "Omit system fields (account_id, session_id, confirmed)."
                            ),
                        },
                        "justification": {
                            "type": "string",
                            "description": "One sentence explaining why this tool was selected.",
                        },
                    },
                    "required": ["tool_name", "tool_input", "justification"],
                },
                "minItems": 1,
            },
        },
        "required": ["tools"],
    },
}

_SUCCESS_SYSTEM = """\
You are a transaction disclosure system. Report the outcome using only the data returned by the tool.
Rules:
- State what was done, to which account, for what amount.
- If a processing timeline is present, include the exact value verbatim (e.g. "3–10 business days").
- No filler phrases ("Great news", "Happy to help", "Feel free to reach out", etc.).
- No assumptions or inferences beyond what the data contains.
- 2–4 sentences maximum. Plain prose, no bullet points.\
"""

_READ_SYSTEM = """\
You are a billing assistant. Present the information to the member concisely and directly.
Rules:
- Answer the member's question using only the data provided.
- Do not mention "the tool", "the result", or what was or was not returned.
- Plain prose. Use a compact list only when presenting multiple transactions.
- No filler phrases. No assumptions beyond what the data contains.\
"""

_SCHEDULING_SYSTEM = """\
You are a friendly appointment desk assistant. A client's scheduling request had a constraint.
Rephrase the following message as a warm, helpful 1-2 sentence response.
Rules:
- If an alternative date is mentioned, offer it as a natural suggestion ("Would you like to book for Wednesday the 17th instead?").
- Use conversational language. Never say "error", "system", "unable to complete", or "not available at this moment" — say "fully booked" or "we're closed" as appropriate.
- Open with a brief acknowledgement like "Sorry," or "Unfortunately," then the helpful substance.
- No filler phrases beyond the opening. No bullet points.\
"""


# ─── Form specifications ──────────────────────────────────────────────────────
# Defines which tools require a form and what fields to collect.
# System fields (account_id, session_id, confirmed) are never in forms.
# "required" controls whether a missing field triggers the form gate.

_FORM_SPECS: dict[str, dict[str, Any]] = {
    "pause_subscription": {
        "fields": [
            {
                "name": "start_date",
                "label": "Start Date",
                "type": "date",
                "required": True,
                "hint": "Must be submitted at least 14 days in advance of this date.",
            },
            {
                "name": "duration_days",
                "label": "Duration (days)",
                "type": "integer",
                "required": True,
                "min_value": 30,
                "max_value": 180,
                "hint": "30–90 days: €10/30-day period fee. 91–180 days: escalation to support required.",
            },
            {
                "name": "end_date",
                "label": "End Date",
                "type": "date",
                "required": True,
            },
            {
                "name": "has_documentation",
                "label": "Fee-exemption documentation available",
                "type": "boolean",
                "required": False,
                "hint": "Medical, military, or job displacement. Your request will be escalated to support — the pause is applied once staff validate your documents. Submit to support@fitness.com.",
            },
        ],
        "lock_group": {
            "fields": ["start_date", "duration_days", "end_date"],
            "recalculate": "duration_days",
        },
    },
    "change_plan": {
        "fields": [
            {
                "name": "new_plan",
                "label": "New Plan",
                "type": "select",
                "required": True,
                "options": [
                    {"value": "standard", "label": "Standard — €60/month · €600/year"},
                    {"value": "premium",  "label": "Premium — €120/month · €1,200/year"},
                ],
            },
            {
                "name": "new_billing_type",
                "label": "Billing Cycle",
                "type": "select",
                "required": True,
                "options": [
                    {"value": "monthly", "label": "Monthly"},
                    {"value": "annual", "label": "Annual"},
                ],
            },
        ],
        "lock_group": None,
    },
}

_SYSTEM_FIELDS = frozenset({"account_id", "session_id", "confirmed"})

_ACTION_TOOLS = frozenset({
    # customer_ops
    "process_refund", "cancel_subscription", "pause_subscription",
    "change_plan", "send_member_notification",
    # appointments
    "book_appointment", "reschedule_appointment", "cancel_appointment",
})

# Key: tool already executed. Value: set of tools blocked as a result.
# Symmetric: every pair that conflicts must be listed in both directions.
_CONFLICTS: dict[str, frozenset[str]] = {
    "pause_subscription": frozenset({"cancel_subscription", "process_refund"}),
    "cancel_subscription": frozenset({"pause_subscription", "process_refund"}),
    "process_refund":      frozenset({"pause_subscription", "cancel_subscription"}),
}


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _trace(state: AgentState, phase: str, data: dict) -> list[dict]:
    """Return the accumulated trace with one new step appended."""
    return (state.get("trace") or []) + [{"phase": phase, "data": data}]


@lru_cache(maxsize=1)
def _get_selection_system() -> str:
    """Backward-compat: selection system for the default domain_pack."""
    return _get_selection_system_for_domain(settings.domain_pack)


@lru_cache(maxsize=8)
def _get_selection_system_for_domain(domain: str) -> str:
    """Build the selection system prompt for a specific domain. Cached per domain."""
    _, selection_base = _get_prompts_for_domain(domain)
    tools = get_tool_names_and_descriptions()
    tool_list = "\n".join(f"- {t['name']}: {t['description']}" for t in tools)
    return f"{selection_base}\n\nRegistered tools:\n{tool_list}"


def _check_missing_params(tool_name: str, tool_input: dict) -> list[str]:
    """Returns names of required fields absent from tool_input for tools with form specs."""
    spec = _FORM_SPECS.get(tool_name, {})
    return [
        f["name"]
        for f in spec.get("fields", [])
        if f.get("required", True) and f["name"] not in tool_input
    ]


def _find_conflicting_action_pairs(tools: list[dict]) -> list[tuple[str, str]]:
    """Return the first pair of action tool names that mutually conflict."""
    action_names = [t["tool_name"] for t in tools if t["tool_name"] in _ACTION_TOOLS]
    for i, a in enumerate(action_names):
        for b in action_names[i + 1:]:
            if b in _CONFLICTS.get(a, frozenset()) or a in _CONFLICTS.get(b, frozenset()):
                return [(a, b)]
    return []


def _describe_entity(entity_id: str, domain: str) -> str | None:
    """
    Return a plain-text entity description from the active domain's tools module.
    Tries describe_entity() first, then describe_account() for backward compat.
    """
    import importlib
    try:
        from domains import load_domain_pack
        pack = load_domain_pack(domain)
        for mod_path in pack.get("tools_modules", []):
            try:
                mod = importlib.import_module(mod_path)
                fn = getattr(mod, "describe_entity", None) or getattr(mod, "describe_account", None)
                if fn:
                    result = fn(entity_id)
                    return result if isinstance(result, str) else None
            except Exception:
                pass
    except Exception:
        pass
    return f"entity_id={entity_id}"


def _classify_intent(input_text: str, account_context: str | None, domain: str | None = None) -> str:
    """Fast LLM call: classify the request as a short descriptive phrase."""
    classification_system = _get_prompts_for_domain(domain or settings.domain_pack)[0]
    context_prefix = f"Account: {account_context}\n\n" if account_context else ""
    resp = llm_call(
        messages=[{"role": "user", "content": f"{context_prefix}Request: {input_text}"}],
        system=classification_system,
        model_tier="fast",
    )
    return resp.get("content", "").strip() or input_text[:80]


def _derive_verified_changes(tool_name: str, result: Any) -> list[str]:
    """
    Extract factual change statements from a tool's execution result.
    Returns [] for read-only tools. Returns ["No system changes applied"] on error.
    """
    if result is None:
        return []

    d: dict = (
        result.model_dump() if hasattr(result, "model_dump")
        else (result if isinstance(result, dict) else {})
    )

    err = d.get("error")
    if err:
        msg = err.get("message", "") if isinstance(err, dict) else str(err)
        return [f"No system changes applied — {msg}" if msg else "No system changes applied"]

    if d.get("confirmation_required"):
        return ["Awaiting member confirmation — no changes applied"]

    if not d.get("success", True):
        return ["No system changes applied"]

    changes: list[str] = []

    if tool_name == "process_refund":
        if d.get("refund_amount") is not None:
            changes.append(
                f"Refunded €{d['refund_amount']:.2f} to {d.get('refund_method', 'original payment method')}"
            )
        if d.get("processing_timeline"):
            changes.append(f"Expected credit arrival: {d['processing_timeline']}")
        if d.get("subscription_status"):
            changes.append(f"Subscription status → {d['subscription_status']}")
        gp = d.get("guest_pass_status")
        if gp and gp not in ("None", "N/A", None):
            changes.append(f"Guest pass → {gp}")

    elif tool_name == "cancel_subscription":
        if d.get("subscription_valid_until"):
            changes.append(
                f"Auto-renewal disabled; subscription valid until {d['subscription_valid_until']}"
            )
        else:
            changes.append("Auto-renewal disabled")
        if d.get("guest_pass_valid_until"):
            changes.append(f"Guest pass valid until {d['guest_pass_valid_until']}")

    elif tool_name == "pause_subscription":
        if d.get("pause_start") and d.get("pause_end"):
            changes.append(f"Subscription paused {d['pause_start']} → {d['pause_end']}")
        fee = d.get("pause_fee")
        if fee is not None and fee > 0:
            changes.append(f"Pause fee charged: €{fee:.2f}")
        if d.get("new_renewal_date"):
            changes.append(f"Renewal date → {d['new_renewal_date']}")
        if d.get("guest_pass_impact"):
            changes.append(f"Guest pass: {d['guest_pass_impact']}")

    elif tool_name == "change_plan":
        if d.get("from_plan") and d.get("to_plan"):
            changes.append(
                f"Plan: {d['from_plan']} → {d['to_plan']} ({d.get('change_type', 'change')})"
            )
        charge = d.get("immediate_charge")
        if charge is not None and charge > 0:
            changes.append(f"Immediate charge: €{charge:.2f}")
        if d.get("effective_date"):
            changes.append(f"Effective: {d['effective_date']}")
        if d.get("guest_pass_impact"):
            changes.append(f"Guest pass: {d['guest_pass_impact']}")

    elif tool_name == "book_appointment":
        pass  # BookResult.message carries the polite confirmation; facts left empty so formatter falls back to it.

    elif tool_name == "reschedule_appointment":
        old_iso = d.get("old_date")
        new_iso = d.get("new_date")
        if old_iso and new_iso:
            try:
                old_fmt = date.fromisoformat(old_iso).strftime("%A, %d %B").replace(" 0", " ")
                new_fmt = date.fromisoformat(new_iso).strftime("%A, %d %B").replace(" 0", " ")
            except (ValueError, TypeError):
                old_fmt, new_fmt = old_iso, new_iso
            changes.append(f"We've moved your appointment from {old_fmt} to {new_fmt}.")
        fee = d.get("reschedule_fee")
        if fee is not None and fee > 0:
            changes.append(f"Please note a same-day rescheduling fee of €{fee:.2f} applies.")

    elif tool_name == "cancel_appointment":
        appt_iso = d.get("appointment_date")
        if appt_iso:
            try:
                appt_fmt = date.fromisoformat(appt_iso).strftime("%A, %d %B").replace(" 0", " ")
            except (ValueError, TypeError):
                appt_fmt = appt_iso
            changes.append(f"Your appointment on {appt_fmt} has been cancelled.")
        fee = d.get("cancellation_fee")
        if fee is not None and fee > 0:
            changes.append(f"A same-day cancellation fee of €{fee:.2f} has been applied.")
        elif d.get("fee_waived") or fee == 0:
            changes.append("No cancellation fee — thank you for giving us enough notice.")

    return changes or (["Operation completed — no account state changes"] if d.get("success") else [])


def _serialise_result(result: Any) -> Any:
    """
    Safely convert a tool result to a JSON-compatible value for trace and log storage.
    Pydantic models use model_dump(mode="json") so dates/enums are serialised correctly.
    """
    if result is None:
        return None
    if hasattr(result, "model_dump"):
        return result.model_dump(mode="json")
    if isinstance(result, dict):
        try:
            # Round-trip through JSON to catch any non-serialisable values
            return json.loads(json.dumps(result, default=str))
        except Exception:
            return {k: str(v) for k, v in result.items()}
    try:
        return json.loads(json.dumps(result, default=str))
    except Exception:
        return str(result)


# ─── Node: interpretation ─────────────────────────────────────────────────────

def interpretation_node(state: AgentState) -> dict:
    """
    Load session history and account context.
    Classify the user's intent via a fast LLM call (skipped when confirmed is not None).
    On confirmed=True: load pending confirmation, merge form_data if pending_type="form".
    On confirmed=False: cancel pending, route to respond.
    """
    history = memory.build_context(
        state["session_id"],
        state.get("allow_history_reference", True),
    )
    confirmed = state.get("confirmed")

    account_id = state.get("account_id")
    domain_name = state.get("domain") or settings.domain_pack
    account_context: str | None = None
    if account_id:
        account_context = _describe_entity(account_id, domain_name)

    updates: dict[str, Any] = {
        "history": history,
        "available_tools": [],
        "iterations": state.get("iterations", 0),
        "account_context": account_context,
        "error": None,
        "halt_reason": None,
        "selected_tool": None,
        "tool_input_data": None,
        "classification": None,
        "selection_justification": None,
        "form_spec": None,
        "verified_changes": None,
        "confirmation_value": None,
    }

    if confirmed is not None:
        pending = crud.get_pending_confirmation_data(state["session_id"])
        if pending is None:
            # TTL elapsed or no confirmation was ever stored.
            updates["halt_reason"] = "confirmation_expired"
            updates["intent"] = "confirmation_expired"
            updates["classification"] = "Confirmation expired"
            updates["trace"] = _trace(state, "interpretation", {
                "input": state["input"],
                "classification": "confirmation_expired",
                "account_id": account_id,
                "history_length": len(history),
                "confirmed": confirmed,
            })
            return updates
        if pending:
            tool_name, payload_dict, input_data = pending
            if confirmed is True:
                crud.resolve_pending_confirmation(state["session_id"])

                if payload_dict.get("_pending_type") == "form":
                    # Form submitted — merge fields and set confirmed=True.
                    # The form's live preview section replaces the separate confirmation screen,
                    # so the user has already reviewed what will change before clicking Confirm.
                    form_data = state.get("form_data") or {}
                    input_data = {**input_data, **form_data}

                    all_tools: list[dict] = list(payload_dict.get("_selected_tools") or [])
                    if not all_tools:
                        all_tools = [{
                            "tool_name": tool_name,
                            "tool_input": {**input_data, "confirmed": True},
                            "justification": payload_dict.get("_selection_justification") or "",
                        }]
                    else:
                        # Merge form_data + confirmed=True into the matching tool entry (first match)
                        merged = False
                        for i, t in enumerate(all_tools):
                            if t["tool_name"] == tool_name and not merged:
                                all_tools[i] = {
                                    **t,
                                    "tool_input": {**t.get("tool_input", {}), **form_data, "confirmed": True},
                                }
                                merged = True

                    selected_tools = all_tools
                    updates["tool_input_data"] = {**input_data, "confirmed": True}
                else:
                    # Action confirmation: prepend confirmed tool, append remaining
                    confirmed_entry = {
                        "tool_name": tool_name,
                        "tool_input": {
                            **{k: v for k, v in input_data.items() if k not in _SYSTEM_FIELDS},
                            "confirmed": True,
                        },
                        "justification": payload_dict.get("_selection_justification") or "",
                    }
                    remaining = payload_dict.get("_remaining_tools") or []
                    selected_tools = [confirmed_entry] + remaining
                    updates["tool_input_data"] = {**input_data, "confirmed": True}

                updates["selected_tools"] = selected_tools
                updates["selected_tool"] = tool_name
                updates["intent"] = tool_name
                updates["confirmation_value"] = True
                updates["classification"] = f"Confirmed: execute {tool_name}"
                updates["selection_justification"] = payload_dict.get("_selection_justification")
            else:
                crud.cancel_pending_confirmation(state["session_id"])
                updates["halt_reason"] = "cancelled"
                updates["intent"] = "cancelled"
                updates["confirmation_value"] = False
                updates["classification"] = "Cancelled pending action"

    if confirmed is None:
        classification = _classify_intent(state["input"], account_context, domain=domain_name)
        updates["classification"] = classification

    updates["trace"] = _trace(state, "interpretation", {
        "input": state["input"],
        "classification": updates.get("classification"),
        "account_id": account_id,
        "history_length": len(history),
        "confirmed": confirmed,
        "form_data_keys": list((state.get("form_data") or {}).keys()) or None,
    })

    return updates


# ─── Node: selection ──────────────────────────────────────────────────────────

def selection_node(state: AgentState) -> dict:
    """
    One Haiku-tier LLM call using the execute_plan wrapper.

    The LLM sees registered tool names + descriptions (not full schemas) and the
    disambiguation rules. It outputs: tool_name, tool_input (only extractable fields),
    and justification.

    After selection, missing required parameters are detected. If any are missing,
    halt_reason is set to "form_required" and the form gate fires instead of execution.
    """
    iterations = state.get("iterations", 0) + 1

    if iterations > MAX_ITERATIONS:
        return {
            "iterations": iterations,
            "halt_reason": "max_iterations",
            "trace": _trace(state, "selection", {
                "halt": "max_iterations",
                "iterations": iterations,
            }),
        }

    messages = list(state.get("history") or [])
    user_content = state["input"]
    if state.get("account_context"):
        user_content = f"[Active account: {state['account_context']}]\n\n{user_content}"
    messages.append({"role": "user", "content": user_content})

    _domain_name = state.get("domain") or settings.domain_pack
    response = llm_call(
        messages=messages,
        system=_get_selection_system_for_domain(_domain_name),
        tools=[_EXECUTE_PLAN_SCHEMA],
        model_tier="fast",
        force_tool="execute_plan",
    )

    if "error" in response:
        return {
            "iterations": iterations,
            "error": {"code": "LLM_CALL_FAILED", "message": response["error"]},
            "halt_reason": "tool_failure",
            "trace": _trace(state, "selection", {
                "error": response["error"],
                "iterations": iterations,
            }),
        }

    if response.get("stop_reason") == "tool_use" and response.get("tool_use"):
        call = response["tool_use"][0]

        if call["name"] != "execute_plan":
            return {
                "iterations": iterations,
                "error": {
                    "code": "SELECTION_SCHEMA_ERROR",
                    "message": f"LLM called unexpected tool '{call['name']}' instead of execute_plan.",
                },
                "halt_reason": "tool_failure",
                "trace": _trace(state, "selection", {
                    "error": f"unexpected tool: {call['name']}",
                    "iterations": iterations,
                }),
            }

        plan       = call["input"]
        tools_list = plan.get("tools") or []

        if not tools_list:
            clarifying = "Could you please clarify your request?"
            return {
                "iterations": iterations,
                "intent": "ambiguous",
                "halt_reason": "ambiguity",
                "clarifying_question": clarifying,
                "trace": _trace(state, "selection", {
                    "intent": "ambiguous",
                    "clarifying_question": clarifying,
                    "iterations": iterations,
                }),
            }

        # Handle clarify (must be the sole entry)
        if any(t.get("tool_name") == "clarify" for t in tools_list):
            clarifying = next(
                (t.get("justification", "") for t in tools_list if t.get("tool_name") == "clarify"),
                "",
            ) or "Could you please clarify your request?"
            return {
                "iterations": iterations,
                "intent": "ambiguous",
                "halt_reason": "ambiguity",
                "clarifying_question": clarifying,
                "selection_justification": clarifying,
                "trace": _trace(state, "selection", {
                    "intent": "ambiguous",
                    "clarifying_question": clarifying,
                    "iterations": iterations,
                }),
            }

        # Strip system fields from each tool's input
        cleaned_tools = [
            {
                "tool_name": t.get("tool_name", ""),
                "tool_input": {
                    k: v for k, v in (t.get("tool_input") or {}).items()
                    if k not in _SYSTEM_FIELDS
                },
                "justification": t.get("justification", ""),
            }
            for t in tools_list
        ]

        # Detect conflicting action tools — ask the member to choose before executing anything
        conflicting_pairs = _find_conflicting_action_pairs(cleaned_tools)
        if conflicting_pairs:
            a, b = conflicting_pairs[0]
            clarifying = (
                f"Your request includes a {a.replace('_', ' ')} and a "
                f"{b.replace('_', ' ')} — these actions conflict and cannot both be "
                f"applied to the same account. Which would you like to proceed with?"
            )
            return {
                "iterations": iterations,
                "intent": "conflicting_actions",
                "halt_reason": "ambiguity",
                "clarifying_question": clarifying,
                "selection_justification": clarifying,
                "trace": _trace(state, "selection", {
                    "intent": "conflicting_actions",
                    "conflicting_pair": [a, b],
                    "all_planned_tools": [t["tool_name"] for t in cleaned_tools],
                    "iterations": iterations,
                }),
            }

        # Check missing required params — trigger form for the first tool that needs one
        for tool_entry in cleaned_tools:
            missing = _check_missing_params(tool_entry["tool_name"], tool_entry["tool_input"])
            if missing:
                return {
                    "iterations": iterations,
                    "intent": tool_entry["tool_name"],
                    "selected_tool": tool_entry["tool_name"],
                    "selected_tools": cleaned_tools,
                    "tool_input_data": tool_entry["tool_input"],
                    "selection_justification": tool_entry["justification"],
                    "halt_reason": "form_required",
                    "trace": _trace(state, "selection", {
                        "selected_tools": [t["tool_name"] for t in cleaned_tools],
                        "form_tool": tool_entry["tool_name"],
                        "missing_params": missing,
                        "extracted_params": list(tool_entry["tool_input"].keys()),
                        "iterations": iterations,
                    }),
                }

        return {
            "iterations": iterations,
            "intent": cleaned_tools[0]["tool_name"],
            "selected_tool": cleaned_tools[0]["tool_name"],
            "selected_tools": cleaned_tools,
            "tool_input_data": cleaned_tools[0]["tool_input"],
            "selection_justification": cleaned_tools[0]["justification"],
            "halt_reason": None,
            "trace": _trace(state, "selection", {
                "selected_tools": [t["tool_name"] for t in cleaned_tools],
                "extracted_params_per_tool": {
                    t["tool_name"]: list(t["tool_input"].keys()) for t in cleaned_tools
                },
                "iterations": iterations,
            }),
        }

    # LLM returned text without calling execute_plan — treat as ambiguity
    clarifying = response.get("content") or "Could you please clarify your request?"
    return {
        "iterations": iterations,
        "intent": "ambiguous",
        "halt_reason": "ambiguity",
        "clarifying_question": clarifying,
        "trace": _trace(state, "selection", {
            "intent": "ambiguous",
            "clarifying_question": clarifying,
            "iterations": iterations,
        }),
    }


# ─── Node: form ───────────────────────────────────────────────────────────────

def form_node(state: AgentState) -> dict:
    """
    Required parameters are missing. Build the form spec (pre-filling any extracted values),
    persist the partial state as a pending confirmation, and return halt_reason="form_required".

    The frontend renders the form. On submission, the user sends confirmed=True + form_data,
    which interpretation_node merges back into tool_input_data before routing to execution.
    """
    tool_name  = state.get("selected_tool", "")
    tool_input = state.get("tool_input_data") or {}
    raw_spec   = _FORM_SPECS.get(tool_name, {})

    # Build field list with pre-filled values from what the LLM already extracted
    fields_out: list[dict] = []
    for f in raw_spec.get("fields", []):
        entry = dict(f)
        val = tool_input.get(f["name"])
        if val is not None:
            entry["prefilled"] = str(val)
        fields_out.append(entry)

    form_spec = {
        "tool_name": tool_name,
        "fields": fields_out,
        "lock_group": raw_spec.get("lock_group"),
    }

    # Store partial input so confirmed=True can resume from here
    stored_input: dict[str, Any] = {
        **(tool_input or {}),
        "session_id": state["session_id"],
    }
    if state.get("account_id"):
        stored_input["account_id"] = state["account_id"]

    # Embed account data for client-side live preview (pause_subscription only)
    if tool_name == "pause_subscription" and state.get("account_id"):
        try:
            from tools.customer_ops import get_pause_preview_context
            ctx = get_pause_preview_context(state["account_id"])
            if ctx:
                form_spec["preview_context"] = ctx
        except Exception:
            pass

    crud.create_pending_confirmation(
        session_id=state["session_id"],
        tool_name=tool_name,
        payload={
            "_pending_type": "form",
            "_selection_justification": state.get("selection_justification"),
            "_selected_tools": state.get("selected_tools") or [],
        },
        input_data=stored_input,
    )

    missing = [f["name"] for f in fields_out if "prefilled" not in f and f.get("required", True)]

    return {
        "form_spec": form_spec,
        "trace": _trace(state, "form", {
            "tool_name": tool_name,
            "missing_fields": missing,
            "prefilled_fields": [f["name"] for f in fields_out if "prefilled" in f],
        }),
    }


# ─── Node: execution ──────────────────────────────────────────────────────────

def execution_node(state: AgentState) -> dict:
    """
    Execute all selected tools sequentially.

    Behaviour per tool:
    - Error (ToolError)         → record failure, continue to next tool
    - confirmation_required     → halt, store remaining tools in pending, stop loop
    - Success                   → record result, continue to next tool

    Overall halt_reason:
    - "confirmation_pending" if halted for confirmation
    - "success" if all tools were attempted (failures are reported inline)
    - "tool_failure" only when exactly one tool failed and nothing succeeded
    """
    selected_tools: list[dict] = list(state.get("selected_tools") or [])
    if not selected_tools:
        # Backward compat: single-tool path
        selected_tools = [{
            "tool_name": state.get("selected_tool") or "",
            "tool_input": dict(state.get("tool_input_data") or {}),
            "justification": state.get("selection_justification") or "",
        }]

    # ── Dry-run short-circuit ──────────────────────────────────────────────────
    # Simulate execution without calling any tool.  Returns what *would* have run.
    if state.get("dry_run"):
        simulated = [
            {"tool_name": t["tool_name"], "halt_reason": "dry_run", "simulated": True}
            for t in selected_tools
        ]
        names = [t["tool_name"] for t in selected_tools]
        return {
            "tool_result":    {"dry_run": True, "would_execute": names},
            "tool_results":   simulated,
            "error":          None,
            "halt_reason":    "dry_run",
            "confirmation_payload": None,
            "verified_changes": ["[Dry run] No changes applied — simulation mode."],
            "trace": _trace(state, "execution", {
                "dry_run":         True,
                "simulated_tools": names,
            }),
        }

    tool_results: list[dict] = []
    aggregate_verified: list[str] = []
    halt_reason = "success"
    error: dict | None = None
    confirmation_payload: dict | None = None
    last_result: Any = None
    executed_action_tools: set[str] = set()  # tracks confirmed+executed action tools this run

    for idx, entry in enumerate(selected_tools):
        tool_name  = entry["tool_name"]
        input_data = dict(entry.get("tool_input") or {})
        input_data["session_id"] = state["session_id"]
        if state.get("account_id"):
            input_data["account_id"] = state["account_id"]

        # Conflict guard: block any tool that contradicts an already-confirmed action
        conflicted_by = {
            a for a in executed_action_tools
            if tool_name in _CONFLICTS.get(a, frozenset())
        }
        if conflicted_by:
            blocker = next(iter(conflicted_by))
            tool_results.append({
                "tool_name": tool_name,
                "halt_reason": "blocked",
                "blocker": blocker,
            })
            continue

        result    = execute_tool(tool_name, input_data)
        last_result = result

        # ── Error ──────────────────────────────────────────────────────────
        if isinstance(result, dict) and "error" in result:
            # Registry-level error (TOOL_NOT_FOUND, TOOL_INPUT_VALIDATION_FAILED).
            # No tool body ran — no account snapshot or policy checks to capture.
            raw_err = result["error"]
            err = raw_err if isinstance(raw_err, dict) else vars(raw_err)
            tool_results.append({
                "tool_name": tool_name,
                "halt_reason": "tool_failure",
                "error": err,
                "input_debug": {k: v for k, v in input_data.items() if k not in _SYSTEM_FIELDS},
            })
            error = err
            # continue to next tool

        elif hasattr(result, "error") and result.error is not None:
            # Policy-layer error: tool ran its checks and returned a ToolError.
            # The result object also carries debug.account_snapshot + debug.policy_checks —
            # serialise the full result so the trace shows which check failed and what it saw.
            raw_err = result.error
            err = raw_err if isinstance(raw_err, dict) else raw_err.model_dump()
            tool_results.append({
                "tool_name": tool_name,
                "halt_reason": "tool_failure",
                "error": err,
                "result_serialised": _serialise_result(result),
                "input_debug": {k: v for k, v in input_data.items() if k not in _SYSTEM_FIELDS},
            })
            error = err
            # continue to next tool

        # ── Confirmation required — halt, store remaining ──────────────────
        elif hasattr(result, "confirmation_required") and result.confirmation_required:
            cp = getattr(result, "confirmation_payload", None)
            cp_dict = cp.model_dump() if cp is not None else {}
            confirmation_payload = cp_dict

            stored_payload = {
                **cp_dict,
                "_pending_type": "action",
                "_selection_justification": entry.get("justification"),
                "_remaining_tools": selected_tools[idx + 1:],
                "_completed_results": [
                    {k: v for k, v in r.items() if k not in ("result", "result_serialised")}
                    for r in tool_results
                ],
            }
            crud.create_pending_confirmation(
                session_id=state["session_id"],
                tool_name=tool_name,
                payload=stored_payload,
                input_data={
                    **{k: v for k, v in input_data.items() if k not in _SYSTEM_FIELDS},
                    "session_id": state["session_id"],
                },
            )
            tool_results.append({
                "tool_name": tool_name,
                "halt_reason": "confirmation_pending",
                "result_serialised": _serialise_result(result),  # carries debug.policy_checks
                "input_debug": {k: v for k, v in input_data.items() if k not in _SYSTEM_FIELDS},
            })
            halt_reason = "confirmation_pending"
            break

        # ── Success ────────────────────────────────────────────────────────
        else:
            verified = _derive_verified_changes(tool_name, result)
            aggregate_verified.extend(verified)
            tool_results.append({
                "tool_name": tool_name,
                "halt_reason": "success",
                "verified_changes": verified,
                "result": result,
                "result_serialised": _serialise_result(result),
                "input_debug": {k: v for k, v in input_data.items() if k not in _SYSTEM_FIELDS},
            })
            if tool_name in _ACTION_TOOLS:
                executed_action_tools.add(tool_name)

    # Resolve overall halt_reason
    if halt_reason != "confirmation_pending":
        successes = [r for r in tool_results if r["halt_reason"] == "success"]
        failures  = [r for r in tool_results if r["halt_reason"] == "tool_failure"]
        # "blocked" entries are policy decisions, not errors — excluded from both counts
        if successes:
            halt_reason = "success"
            if not failures:
                error = None  # clean run — clear any accumulated error
        elif failures:
            halt_reason = "tool_failure"
            error = failures[0].get("error")  # first failure for single-tool formatter

    # Resolved tool_result for backward compat (last successful result, or last result)
    last_success = next((r for r in reversed(tool_results) if r["halt_reason"] == "success"), None)
    tool_result  = last_success["result"] if last_success else last_result

    # verified_changes
    if halt_reason == "confirmation_pending":
        verified_changes: list[str] | None = ["Awaiting member confirmation — no changes applied"]
    elif aggregate_verified:
        verified_changes = aggregate_verified
    elif halt_reason == "tool_failure":
        verified_changes = ["No system changes applied"]
    else:
        verified_changes = None

    # Build enriched trace entries — include input, error, verified_changes, and result
    # per tool so the log has full debugging data without needing to re-run.
    enriched_results = []
    for r in tool_results:
        entry: dict[str, Any] = {
            "tool": r["tool_name"],
            "status": r["halt_reason"],
        }
        if r.get("input_debug"):
            entry["input"] = r["input_debug"]
        if r.get("error"):
            entry["error"] = r["error"]
        if r.get("verified_changes"):
            entry["verified_changes"] = r["verified_changes"]
        if r.get("result_serialised") is not None:
            entry["result"] = r["result_serialised"]
        if r.get("blocker"):
            entry["blocker"] = r["blocker"]
        enriched_results.append(entry)

    return {
        "tool_result": tool_result,
        "tool_results": tool_results,
        "error": error,
        "halt_reason": halt_reason,
        "confirmation_payload": confirmation_payload,
        "verified_changes": verified_changes,
        "trace": _trace(state, "execution", {
            "tools_attempted": [r["tool_name"] for r in tool_results],
            "results": enriched_results,
            "overall_halt_reason": halt_reason,
        }),
    }


# ─── Node: log ────────────────────────────────────────────────────────────────

def log_node(state: AgentState) -> dict:
    """Persist the full run record to the database."""
    result = state.get("tool_result")
    serialisable: Any = None
    if result is not None:
        if hasattr(result, "model_dump"):
            serialisable = result.model_dump()
        elif isinstance(result, dict):
            serialisable = result

    log_id = log_run(
        session_id=state["session_id"],
        input=state["input"],
        intent=state.get("classification") or state.get("intent"),
        selected_tool=state.get("selected_tool"),
        reasoning=state.get("classification"),
        available_tools=[t["name"] for t in state.get("available_tools") or []],
        selection_justification=state.get("selection_justification"),
        expected_result=None,
        constraint_validation=None,
        tool_result=serialisable,
        error=state.get("error"),
        iterations_used=state.get("iterations", 1),
        halt_reason=state.get("halt_reason"),
        trace=list(state.get("trace") or []),
    )

    # Build per-tool summary: exclude raw result object, keep serialised version
    # and include input_debug so the log record has full debugging data.
    tool_results_summary = [
        {k: v for k, v in r.items() if k not in ("result",)}
        for r in (state.get("tool_results") or [])
    ]
    planned_tools = [t["tool_name"] for t in (state.get("selected_tools") or [])]

    log_trace: dict[str, Any] = {
        "log_id": log_id,
        "input": state["input"],
        "classification": state.get("classification"),
        "planned_tools": planned_tools or ([state.get("selected_tool")] if state.get("selected_tool") else []),
        "selected_tool": state.get("selected_tool"),
        "justification": state.get("selection_justification"),
        "halt_reason": state.get("halt_reason"),
        "confirmation_value": state.get("confirmation_value"),
        "verified_changes": state.get("verified_changes"),
        "tool_results": tool_results_summary or None,
    }
    if state.get("form_spec"):
        log_trace["form_required"] = True
        log_trace["form_tool"] = state["form_spec"].get("tool_name")

    return {"trace": _trace(state, "log", log_trace)}


# ─── Node: respond ────────────────────────────────────────────────────────────

def respond_node(state: AgentState) -> dict:
    """
    Format the final response and persist both conversation turns to session history.
    Dispatches to a formatter based on halt_reason.
    Confirmation and error messages are template-formatted (no LLM call).
    Success responses for read-only tools use a fast-tier LLM call.
    """
    halt_reason = state.get("halt_reason")

    if halt_reason == "success":
        response = _fmt_success(state)
    elif halt_reason == "confirmation_pending":
        response = _fmt_confirmation(state)
    elif halt_reason == "form_required":
        response = _fmt_form_required(state)
    elif halt_reason == "tool_failure":
        response = _fmt_error(state)
    elif halt_reason == "ambiguity":
        response = state.get("clarifying_question") or "Could you please clarify your request?"
    elif halt_reason == "max_iterations":
        response = (
            "Unable to determine the correct action after several attempts. "
            "Please contact support for assistance."
        )
    elif halt_reason == "cancelled":
        response = "Request cancelled. No changes were made to your account."
    elif halt_reason == "confirmation_expired":
        response = (
            "Your confirmation window has expired — no changes were made. "
            "Please resubmit your request to start again."
        )
    elif halt_reason == "rate_limited":
        error = state.get("error") or {}
        msg   = error.get("message", "Rate limit reached. Please try again later.")
        response = f"{msg} Contact support@fitness.com for assistance."
    elif halt_reason == "dry_run":
        results  = state.get("tool_results") or []
        names    = [r["tool_name"] for r in results if r.get("tool_name")]
        tool_str = ", ".join(names) if names else "the requested action"
        response = f"[Dry run] No changes were made. Would have executed: {tool_str}."
    else:
        response = "Unable to process your request. Please try again."

    memory.append_turn(state["session_id"], "user", state["input"])
    memory.append_turn(state["session_id"], "assistant", response)

    return {"final_response": response}


# ─── Response formatters ──────────────────────────────────────────────────────

def _fmt_tool_result(tool_name: str, r: dict, user_input: str) -> str | None:
    """Format a single tool's outcome for inclusion in the response. Returns None to skip."""
    if r["halt_reason"] == "tool_failure":
        err = r.get("error") or {}
        msg = err.get("message", "Unable to process request.")
        return msg

    if r["halt_reason"] == "blocked":
        raw_blocker = r.get("blocker", "a previously confirmed action")
        blocker_label = raw_blocker.replace("_", " ")
        action_label = tool_name.replace("_", " ")
        return (
            f"The {action_label} request was not processed — "
            f"a {blocker_label} was already confirmed and applied in this request."
        )

    # success
    verified = r.get("verified_changes") or []
    result = r.get("result")
    answer = (
        getattr(result, "answer", None)
        or getattr(result, "message", None)
        or (result.get("answer") or result.get("message") if isinstance(result, dict) else None)
    ) if result is not None else None

    if tool_name in _ACTION_TOOLS:
        facts = [c for c in verified if not c.startswith("Awaiting") and not c.startswith("No system") and not c.startswith("Operation completed")]
        if facts:
            return ". ".join(f.rstrip(".") for f in facts) + "."
        return answer if answer else None

    if result is None:
        return None

    if answer:
        return answer

    # Structured data (e.g. payment history) — ask the LLM to present it
    result_text = json.dumps(
        result.model_dump() if hasattr(result, "model_dump") else result,
        default=str, indent=2,
    )
    resp = llm_call(
        messages=[{
            "role": "user",
            "content": f"Member question: {user_input}\n\nData:\n{result_text}",
        }],
        system=_READ_SYSTEM,
        model_tier="fast",
    )
    return resp.get("content") or None


def _fmt_success(state: AgentState) -> str:
    tool_results = state.get("tool_results") or []

    if len(tool_results) <= 1:
        # Single-tool path (backward compat)
        tool_name = state.get("selected_tool", "")
        verified  = state.get("verified_changes") or []

        result = state.get("tool_result")

        # Pre-formed natural-language answer (e.g. availability offer, FAQ, no-date booking)
        answer = (
            getattr(result, "answer", None)
            or getattr(result, "message", None)
            or (result.get("answer") or result.get("message") if isinstance(result, dict) else None)
        ) if result is not None else None

        if tool_name in _ACTION_TOOLS:
            facts = [c for c in verified if not c.startswith("Awaiting") and not c.startswith("No system") and not c.startswith("Operation completed")]
            if facts:
                return ". ".join(f.rstrip(".") for f in facts) + "."
            # No real state change — return pre-formed message if present
            return answer if answer else "Request processed."

        if result is None:
            return "Done."

        if answer:
            return answer

        # Structured data — ask the LLM to present it
        result_text = json.dumps(
            result.model_dump() if hasattr(result, "model_dump") else result,
            default=str, indent=2,
        )
        resp = llm_call(
            messages=[{
                "role": "user",
                "content": f"Member question: {state['input']}\n\nData:\n{result_text}",
            }],
            system=_READ_SYSTEM,
            model_tier="fast",
        )
        return resp.get("content") or "Request processed."

    # Multi-tool path
    parts: list[str] = []
    for r in tool_results:
        formatted = _fmt_tool_result(r["tool_name"], r, state["input"])
        if formatted:
            parts.append(formatted)
    return "\n\n".join(parts) if parts else "Request processed."


_CONFIRMATION_LABELS: dict[str, str] = {
    "billing_impact": "Billing impact",
    "member_impact": "Membership",
    "guest_pass_impact": "Guest pass",
    "subscription_validity_impact": "Subscription",
    "fee_impact": "Fee",
    "appointment_date": "Appointment date",
    "old_date": "Current appointment",
    "new_date": "Rescheduled to",
    "reschedule_fee": "Rescheduling fee",
    "cancellation_fee": "Cancellation fee",
    "price": "Session fee",
}

_CONFIRMATION_SKIP = frozenset({"action", "summary", "raw"})


def _fmt_confirmation(state: AgentState) -> str:
    p = state.get("confirmation_payload") or {}
    summary = p.get("summary", "Please confirm the following action.")
    lines: list[str] = [summary]
    details: list[str] = []
    for key, val in p.items():
        if key.startswith("_") or key in _CONFIRMATION_SKIP:
            continue
        if val is None or val == "N/A" or val == "":
            continue
        label = _CONFIRMATION_LABELS.get(key, key.replace("_", " ").title())
        details.append(f"  {label}: {val}")
    if details:
        lines.append("")
        lines.extend(details)
    return "\n".join(lines)


def _fmt_form_required(state: AgentState) -> str:
    tool_name = state.get("selected_tool", "the requested action")
    return (
        f"Additional details are needed to proceed. "
        f"Please complete the form below and confirm."
    )


def _fmt_error(state: AgentState) -> str:
    error = state.get("error") or {}
    code = error.get("code", "")
    message = error.get("message", "An error occurred processing your request.")

    _SCHEDULING_CONSTRAINTS = {
        "DATE_NOT_AVAILABLE", "CLOSED_DAY", "DATE_IN_PAST", "INVALID_DATE",
        "APPOINTMENT_NOT_FOUND", "ALREADY_CANCELLED_APPT", "DUPLICATE_BOOKING",
        "NO_ACTIVE_APPOINTMENT", "MAX_ADVANCE_BOOKING_EXCEEDED", "PENDING_RESCHEDULE_ACTIVE",
    }
    if code in _SCHEDULING_CONSTRAINTS:
        resp = llm_call(
            messages=[{"role": "user", "content": message}],
            system=_SCHEDULING_SYSTEM,
            model_tier="fast",
        )
        return resp.get("content") or message

    _USER_FRIENDLY = {
        "REFUND_WINDOW_EXPIRED", "REFUND_ESCALATION_REQUIRED",
        "PENDING_OPERATION_ACTIVE", "ALREADY_CANCELLED", "ALREADY_PAUSED",
        "PAUSE_NOTICE_INSUFFICIENT", "PAUSE_DURATION_DENIED",
        "PAUSE_ESCALATION_REQUIRED", "PAUSE_DOCUMENTATION_ESCALATION",
        "ACCOUNT_PAUSED", "ACCOUNT_NOT_FOUND", "NO_ACTIVE_SUBSCRIPTION",
        "SLOT_NOT_AVAILABLE", "INSUFFICIENT_NOTICE",
    }
    if code in _USER_FRIENDLY:
        return message
    return f"Something went wrong. {message} If this persists, please contact support."


# ─── Routing ──────────────────────────────────────────────────────────────────

def _route_interpretation(state: AgentState) -> str:
    if state.get("halt_reason") in ("cancelled", "confirmation_expired"):
        return "respond"
    if state.get("selected_tool") and state.get("tool_input_data") is not None:
        return "execution"
    return "selection"


def _route_selection(state: AgentState) -> str:
    halt = state.get("halt_reason")
    if halt in ("ambiguity", "max_iterations", "tool_failure"):
        return "log"
    if halt == "form_required":
        return "form"
    # All successful selections pass through the safety gate before execution.
    return "safety"


# ─── Node: safety ────────────────────────────────────────────────────────────
# Sits between selection and execution.  Demonstrates that the safety gate is
# independently extensible — new checks are added here without touching any
# other node.
#
# Current checks:
#   1. Per-session rate limit for destructive action tools.
#
# When confirmed=True, interpretation_node routes directly to execution (bypassing
# this node).  Rate limits are enforced at plan time, not confirmation time, which
# is the correct UX — a user who was below the limit when they confirmed should
# not be blocked mid-flow.

def safety_node(state: AgentState) -> dict:
    """
    Safety gate: rate-limit check for destructive action tools.

    Passes the state through unchanged when all checks clear.
    Sets halt_reason="rate_limited" and returns to log/respond if a limit is hit.
    """
    from core.safety import RateLimitExceeded, check_rate_limit

    selected_tools: list[dict] = list(state.get("selected_tools") or [])

    for entry in selected_tools:
        tool_name = entry.get("tool_name", "")
        if tool_name not in _ACTION_TOOLS:
            continue
        try:
            check_rate_limit(
                session_id=state["session_id"],
                tool_name=tool_name,
                max_per_session=settings.rate_limit_per_session,
            )
        except RateLimitExceeded as exc:
            return {
                "halt_reason": "rate_limited",
                "error": {
                    "code": exc.code,
                    "message": str(exc),
                    "details": {
                        "tool":            exc.tool_name,
                        "count":           exc.count,
                        "max_per_session": exc.max_per_session,
                        "session_id":      state["session_id"],
                    },
                },
                "trace": _trace(state, "safety", {
                    "rate_limited_tool": tool_name,
                    "halt":              "rate_limited",
                }),
            }

    # All checks passed — return nothing (state is unchanged, only trace appended)
    action_tools_checked = [
        e["tool_name"] for e in selected_tools if e.get("tool_name") in _ACTION_TOOLS
    ]
    return {
        "trace": _trace(state, "safety", {
            "passed":         True,
            "tools_checked":  action_tools_checked,
        }),
    }


def _route_safety(state: AgentState) -> str:
    if state.get("halt_reason") == "rate_limited":
        return "log"
    return "execution"


# ─── Graph ────────────────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def build_graph():
    """
    Assemble and compile the LangGraph state machine.
    Cached for the process lifetime.
    """
    graph = StateGraph(AgentState)

    graph.add_node("interpretation", interpretation_node)
    graph.add_node("selection",      selection_node)
    graph.add_node("safety",         safety_node)
    graph.add_node("form",           form_node)
    graph.add_node("execution",      execution_node)
    graph.add_node("log",            log_node)
    graph.add_node("respond",        respond_node)

    graph.set_entry_point("interpretation")

    graph.add_conditional_edges(
        "interpretation", _route_interpretation,
        # confirmed=True bypasses safety (rate limit is checked at plan time)
        {"selection": "selection", "execution": "execution", "respond": "respond"},
    )
    graph.add_conditional_edges(
        "selection", _route_selection,
        # success path now routes through the safety gate before execution
        {"safety": "safety", "form": "form", "log": "log"},
    )
    graph.add_conditional_edges(
        "safety", _route_safety,
        {"execution": "execution", "log": "log"},
    )

    graph.add_edge("form",      "log")
    graph.add_edge("execution", "log")
    graph.add_edge("log",       "respond")
    graph.add_edge("respond",   END)

    return graph.compile()
