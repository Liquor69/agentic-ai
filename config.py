"""
config.py

Environment configuration and unified LLM client abstraction.

Invariants enforced here:
  - This is the ONLY file in the project that imports from anthropic, openai, or any LLM SDK.
  - All LLM calls from all modules go through llm_call().
  - Provider switching (Anthropic → OpenAI) requires changing LLM_PROVIDER in .env only.

Caching strategy (Anthropic prompt caching):
  - System prompts are always marked cache_control=ephemeral (stable across all calls).
  - Prior conversation turns are marked as a cache breakpoint (stable within a session).
  - The current user message is never cached (always dynamic).
  - Prefix ordering: cached blocks always precede dynamic content.
  - TTL: 5 minutes (Anthropic default). Cache write surcharge: 25% — breakeven at 2 calls/session.
"""

from __future__ import annotations

import logging
import threading
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict

_ROOT = Path(__file__).parent  # project root, regardless of working directory

logger = logging.getLogger(__name__)


# ─── Settings ─────────────────────────────────────────────────────────────────

class Settings(BaseSettings):
    # LLM provider — switch here to change the entire provider
    llm_provider: str = "anthropic"

    # Provider API keys
    anthropic_api_key: str = ""
    openai_api_key: str = ""

    # Model tiers
    # "fast"     → Haiku  — classification, routing, FAQ lookup
    # "standard" → Sonnet — reasoning, planning, response generation
    model_fast: str = "claude-haiku-4-5-20251001"
    model_standard: str = "claude-sonnet-4-6"

    # Database
    database_url: str = "sqlite:///./agent.db"

    # Domain pack — selects which tool set and routing rules are active.
    # Corresponds to a module under domains/ (e.g. "customer_ops" → domains/customer_ops.py).
    domain_pack: str = "customer_ops"

    # Agent behaviour
    max_iterations: int = 5
    session_ttl_seconds: int = 3600
    cache_ttl_seconds: int = 300          # Anthropic prompt cache TTL (5 min)
    confirmation_ttl_seconds: int = 600   # pending-confirmation TTL (10 min)

    # External integrations
    n8n_webhook_base_url: str = ""
    external_api_base_url: str = ""    # billing / external data backend (tools/external_api.py)
    external_api_token: str = ""

    # App
    app_env: str = "development"
    log_level: str = "INFO"

    # Static API key for X-API-Key header auth (FastAPI dependency)
    api_key: str = ""

    model_config = SettingsConfigDict(env_file=str(_ROOT / ".env"), extra="ignore")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings: Settings = get_settings()


# ─── Per-thread token accumulator ────────────────────────────────────────────
# Each run_agent call resets the counter at entry and reads it at exit.
# threading.local() gives each OS thread its own counter, so concurrent requests
# (each running in asyncio.to_thread) don't interfere with each other.

_token_local: threading.local = threading.local()


def reset_token_counter() -> None:
    """Reset the per-thread token accumulator. Called once at the start of each run."""
    _token_local.usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }


def _accumulate_usage(usage: dict[str, int]) -> None:
    """Add LLM call token counts to the running total for this thread."""
    if not hasattr(_token_local, "usage"):
        reset_token_counter()
    for key in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
        _token_local.usage[key] = _token_local.usage.get(key, 0) + usage.get(key, 0)


def get_token_usage() -> dict[str, int]:
    """Return a snapshot of accumulated token counts for the current thread."""
    return dict(getattr(_token_local, "usage", {}))


# ─── Provider client singletons ───────────────────────────────────────────────
# Clients are lazily instantiated and then reused — not recreated per call.

_anthropic_client: Any = None
_openai_client: Any = None


def _get_anthropic_client() -> Any:
    global _anthropic_client
    if _anthropic_client is None:
        try:
            import anthropic
            _anthropic_client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        except ImportError:
            raise RuntimeError("anthropic package not installed. Run: pip install anthropic")
    return _anthropic_client


def _get_openai_client() -> Any:
    global _openai_client
    if _openai_client is None:
        try:
            import openai
            _openai_client = openai.OpenAI(api_key=settings.openai_api_key)
        except ImportError:
            raise RuntimeError("openai package not installed. Run: pip install openai")
    return _openai_client


# ─── Response normalisation ───────────────────────────────────────────────────
# Both providers are normalised to the same dict shape so callers never branch on provider.

def _normalise_anthropic(response: Any) -> dict[str, Any]:
    """
    Normalise an anthropic.types.Message to the shared return format:
    {
        "content":     str            — first text block (empty if none)
        "tool_use":    list[dict]     — [{id, name, input}, ...]
        "stop_reason": str            — "end_turn" | "tool_use" | "max_tokens"
        "usage":       dict           — token counts including cache metrics
    }
    """
    text = ""
    tool_use: list[dict[str, Any]] = []

    for block in response.content:
        if block.type == "text":
            text = block.text
        elif block.type == "tool_use":
            tool_use.append({"id": block.id, "name": block.name, "input": block.input})

    u = getattr(response, "usage", None)
    usage: dict[str, int] = {}
    if u is not None:
        usage = {
            "input_tokens":                getattr(u, "input_tokens", 0),
            "output_tokens":               getattr(u, "output_tokens", 0),
            "cache_read_input_tokens":     getattr(u, "cache_read_input_tokens", 0),
            "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0),
        }

    return {
        "content":     text,
        "tool_use":    tool_use,
        "stop_reason": response.stop_reason,
        "usage":       usage,
    }


def _normalise_openai(response: Any) -> dict[str, Any]:
    """Normalise an openai ChatCompletion to the same shape."""
    import json as _json

    choice = response.choices[0]
    msg = choice.message

    tool_use: list[dict[str, Any]] = []
    if msg.tool_calls:
        for tc in msg.tool_calls:
            tool_use.append({
                "id":    tc.id,
                "name":  tc.function.name,
                "input": _json.loads(tc.function.arguments),
            })

    return {
        "content":     msg.content or "",
        "tool_use":    tool_use,
        "stop_reason": choice.finish_reason,
        "usage": {
            "input_tokens":  response.usage.prompt_tokens,
            "output_tokens": response.usage.completion_tokens,
        },
    }


# ─── Caching helpers ──────────────────────────────────────────────────────────

def _cached_block(block: dict[str, Any]) -> dict[str, Any]:
    return {**block, "cache_control": {"type": "ephemeral"}}


def _build_system_cached(system: str) -> list[dict[str, Any]]:
    """Wrap the system prompt as a single cache-flagged text block."""
    return [_cached_block({"type": "text", "text": system})]


def _build_messages_cached(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Mark stable prior turns with cache_control so the context window is cached
    across calls within the same session.

    Rule:
      - 0 or 1 messages  → no message-level caching (system covers stable context)
      - 2+ messages       → second-to-last message is the cache breakpoint;
                            last message is always dynamic (current user input)

    The last user message is never cached because it is always fresh input.
    """
    if len(messages) <= 1:
        return messages

    result = list(messages)
    idx = len(result) - 2  # second-to-last = most recent stable turn

    msg = dict(result[idx])
    content = msg.get("content")

    if isinstance(content, str):
        msg["content"] = [_cached_block({"type": "text", "text": content})]
    elif isinstance(content, list) and content:
        blocks = list(content)
        blocks[-1] = _cached_block(dict(blocks[-1]))
        msg["content"] = blocks

    result[idx] = msg
    return result


# ─── Unified LLM interface ────────────────────────────────────────────────────

def llm_call(
    messages: list[dict[str, Any]],
    system: str | None = None,
    tools: list[dict[str, Any]] | None = None,
    model_tier: str = "standard",
    force_tool: str | None = None,
) -> dict[str, Any]:
    """
    The sole LLM interface for the entire project.

    Parameters
    ----------
    messages    : Conversation turns. Standard role/content format.
    system      : System prompt. Stable content — will be cache-flagged on Anthropic.
    tools       : Tool schemas in Anthropic tool-use format.
                  Automatically converted to OpenAI format when LLM_PROVIDER=openai.
    model_tier  : "fast"     → Haiku  (classification, routing, FAQ)
                  "standard" → Sonnet (reasoning, planning, response generation)
    force_tool  : When set, forces the model to call this specific tool name
                  (tool_choice={"type": "tool", "name": force_tool}).

    Returns
    -------
    Success: {"content": str, "tool_use": list, "stop_reason": str, "usage": dict}
    Failure: {"error": str}

    All exceptions are caught and returned as {"error": ...} so callers
    never need try/except around llm_call().
    """
    provider = settings.llm_provider.lower()
    try:
        if provider == "anthropic":
            result = _call_anthropic(messages, system, tools, model_tier, force_tool)
        elif provider == "openai":
            result = _call_openai(messages, system, tools, model_tier, force_tool)
        else:
            return {"error": f"Unknown LLM_PROVIDER '{provider}'. Supported: anthropic, openai."}
        _accumulate_usage(result.get("usage") or {})
        return result
    except Exception as exc:
        logger.exception("llm_call failed (provider=%s, tier=%s)", provider, model_tier)
        return {"error": str(exc)}


def _call_anthropic(
    messages: list[dict[str, Any]],
    system: str | None,
    tools: list[dict[str, Any]] | None,
    model_tier: str,
    force_tool: str | None = None,
) -> dict[str, Any]:
    client = _get_anthropic_client()
    model = settings.model_fast if model_tier == "fast" else settings.model_standard

    kwargs: dict[str, Any] = {
        "model":    model,
        "max_tokens": 4096,
        "messages": _build_messages_cached(messages),
    }

    if system:
        kwargs["system"] = _build_system_cached(system)
    if tools:
        kwargs["tools"] = tools
        if force_tool:
            kwargs["tool_choice"] = {"type": "tool", "name": force_tool}

    response = client.messages.create(**kwargs)
    return _normalise_anthropic(response)


def _call_openai(
    messages: list[dict[str, Any]],
    system: str | None,
    tools: list[dict[str, Any]] | None,
    model_tier: str,
    force_tool: str | None = None,
) -> dict[str, Any]:
    client = _get_openai_client()
    model = "gpt-4o-mini" if model_tier == "fast" else "gpt-4o"

    all_messages: list[dict[str, Any]] = []
    if system:
        all_messages.append({"role": "system", "content": system})
    all_messages.extend(messages)

    kwargs: dict[str, Any] = {"model": model, "messages": all_messages}

    if tools:
        # Convert Anthropic tool schema → OpenAI function format
        kwargs["tools"] = [
            {
                "type": "function",
                "function": {
                    "name":        t["name"],
                    "description": t["description"],
                    "parameters":  t["input_schema"],
                },
            }
            for t in tools
        ]
        if force_tool:
            kwargs["tool_choice"] = {"type": "function", "function": {"name": force_tool}}
        else:
            kwargs["tool_choice"] = "auto"

    response = client.chat.completions.create(**kwargs)
    return _normalise_openai(response)
