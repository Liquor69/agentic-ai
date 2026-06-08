"""
tools/registry.py

The extensibility entry point for the agent tool system.

How to add a new tool domain:
  1. Create a file in tools/  (e.g. tools/billing_ops.py)
  2. Import @register_tool and decorate each function
  3. That's it — discover_tools() picks it up at startup

No changes to core/, api/, or any other existing file are required.
The planner never imports tool modules directly — it reads only this registry.
"""

from __future__ import annotations

import importlib
import inspect
import logging
import pkgutil
import typing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel

logger = logging.getLogger(__name__)

# ─── Internal registry storage ────────────────────────────────────────────────

@dataclass
class ToolEntry:
    name: str
    description: str
    input_type: type[BaseModel]
    fn: Callable[..., Any]
    input_schema: dict[str, Any] = field(init=False)

    def __post_init__(self) -> None:
        self.input_schema = self.input_type.model_json_schema()


_REGISTRY: dict[str, ToolEntry] = {}


# ─── Registration decorator ───────────────────────────────────────────────────

def register_tool(fn: Callable[..., Any]) -> Callable[..., Any]:
    """
    Decorator that registers a function as an agent tool.

    Requirements:
      - First parameter must be annotated as a Pydantic BaseModel subclass
      - Must have a docstring; the first line becomes the tool description

    Usage:
        @register_tool
        def my_tool(input: MyToolInput) -> MyToolOutput:
            \"\"\"One-line description used by the planner and GET /tools.\"\"\"
            ...
    """
    sig = inspect.signature(fn)
    param_names = [name for name in sig.parameters if name != "self"]

    if not param_names:
        raise TypeError(
            f"@register_tool: '{fn.__name__}' must accept at least one parameter (the input model)."
        )

    # get_type_hints() resolves stringified annotations produced by
    # `from __future__ import annotations` — inspect.signature() alone returns strings.
    try:
        hints = typing.get_type_hints(fn)
    except Exception as exc:
        raise TypeError(
            f"@register_tool: could not resolve type hints for '{fn.__name__}': {exc}"
        ) from exc

    first_param = param_names[0]
    annotation = hints.get(first_param)

    if annotation is None:
        raise TypeError(
            f"@register_tool: '{fn.__name__}' first parameter '{first_param}' must be "
            "type-annotated with a Pydantic BaseModel subclass."
        )
    if not (isinstance(annotation, type) and issubclass(annotation, BaseModel)):
        raise TypeError(
            f"@register_tool: '{fn.__name__}' first parameter must be a Pydantic BaseModel subclass, "
            f"got {annotation!r}."
        )

    raw_doc = inspect.getdoc(fn) or ""
    description = raw_doc.split("\n")[0].strip()
    if not description:
        raise ValueError(
            f"@register_tool: '{fn.__name__}' must have a docstring "
            "(first line is used as the tool description)."
        )

    _REGISTRY[fn.__name__] = ToolEntry(
        name=fn.__name__,
        description=description,
        input_type=annotation,
        fn=fn,
    )
    logger.debug("Registered tool: %s", fn.__name__)
    return fn


# ─── Registry read interface ──────────────────────────────────────────────────

def get_tool_schemas() -> list[dict[str, Any]]:
    """
    Full tool schemas for the planner.
    Shape matches the Anthropic tool-use API so the planner can pass this
    directly to llm_call() without any transformation.
    """
    return [
        {
            "name": e.name,
            "description": e.description,
            "input_schema": e.input_schema,
        }
        for e in _REGISTRY.values()
    ]


def get_tool_names_and_descriptions() -> list[dict[str, str]]:
    """Minimal list for GET /tools — names and descriptions only, no schemas."""
    return [
        {"name": e.name, "description": e.description}
        for e in _REGISTRY.values()
    ]


def list_tools() -> list[str]:
    """Returns all registered tool names."""
    return list(_REGISTRY.keys())


# ─── Tool execution ───────────────────────────────────────────────────────────

def execute_tool(name: str, input_data: dict[str, Any]) -> Any:
    """
    Look up a registered tool by name, validate its input, and call it.

    Returns the tool's own return value on success.
    Returns a ToolError-shaped dict on: unknown tool, validation failure, or unhandled exception.
    Never raises — all errors are returned as structured dicts so the agent loop
    can handle them uniformly without try/except at the call site.
    """
    entry = _REGISTRY.get(name)
    if entry is None:
        return {
            "error": {
                "code": "TOOL_NOT_FOUND",
                "message": f"No tool named '{name}' is registered.",
                "details": {"available_tools": list(_REGISTRY.keys())},
            }
        }

    try:
        validated = entry.input_type.model_validate(input_data)
    except Exception as exc:
        return {
            "error": {
                "code": "TOOL_INPUT_VALIDATION_FAILED",
                "message": f"Input validation failed for '{name}': {exc}",
                "details": {"raw_input": input_data},
            }
        }

    try:
        return entry.fn(validated)
    except Exception as exc:
        logger.exception("Unhandled exception in tool '%s'", name)
        return {
            "error": {
                "code": "TOOL_EXECUTION_ERROR",
                "message": f"Tool '{name}' raised an unhandled exception: {exc}",
                "details": {},
            }
        }


# ─── Auto-discovery ───────────────────────────────────────────────────────────

_SKIP_MODULES = {"registry", "__init__"}


def discover_tools(tools_dir: Path | None = None) -> None:
    """
    Import every Python module in tools/ except registry.py and __init__.py.
    Modules that use @register_tool self-register on import.

    Called once during app startup (main.py lifespan).
    After this call the planner has a complete tool list with no direct imports.

    Failed imports are logged and skipped — a broken tool file does not
    prevent the rest of the registry from loading.
    """
    if tools_dir is None:
        tools_dir = Path(__file__).parent

    for module_info in pkgutil.iter_modules([str(tools_dir)]):
        if module_info.name in _SKIP_MODULES:
            continue
        full_name = f"tools.{module_info.name}"
        try:
            importlib.import_module(full_name)
            logger.debug("Discovered tool module: %s", full_name)
        except Exception:
            logger.exception("Failed to import tool module '%s' — skipping", full_name)

    logger.info(
        "Tool discovery complete. %d tool(s) registered: %s",
        len(_REGISTRY),
        list(_REGISTRY.keys()),
    )
