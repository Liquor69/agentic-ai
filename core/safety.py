"""
core/safety.py

Input validation and per-session rate-limiting guards.

These are pure predicate functions — they raise typed exceptions on failure and
return cleanly on success.  No routing or decision logic lives here; that stays
in core/planner.py per the architecture invariant.

Call sites:
  validate_input()    — core/agent.py, before graph.invoke()
  check_rate_limit()  — core/planner.safety_node, between selection and execution
"""

from __future__ import annotations

DEFAULT_MAX_INPUT_LENGTH = 2000  # characters; overridden by settings.max_input_length


# ─── Exception hierarchy ──────────────────────────────────────────────────────

class InputRejected(ValueError):
    """Raised when user input fails a pre-graph validation check."""

    def __init__(self, message: str, code: str = "INPUT_REJECTED") -> None:
        super().__init__(message)
        self.code    = code
        self.message = message


class InputTooLong(InputRejected):
    """Raised when user input exceeds the maximum allowed character count."""

    def __init__(self, length: int, max_length: int) -> None:
        super().__init__(
            f"Input is {length} characters — exceeds the {max_length}-character limit. "
            "Please shorten your message.",
            code="INPUT_TOO_LONG",
        )
        self.length     = length
        self.max_length = max_length


class RateLimitExceeded(ValueError):
    """Raised when a session has exceeded the destructive-action rate limit."""

    def __init__(self, tool_name: str, count: int, max_per_session: int) -> None:
        super().__init__(
            f"Session rate limit reached for '{tool_name}' "
            f"({count}/{max_per_session} successful executions this session)."
        )
        self.code            = "RATE_LIMIT_EXCEEDED"
        self.tool_name       = tool_name
        self.count           = count
        self.max_per_session = max_per_session


# ─── Guards ───────────────────────────────────────────────────────────────────

def validate_input(text: str, max_length: int = DEFAULT_MAX_INPUT_LENGTH) -> str:
    """
    Validate and strip user input before starting the agent loop.

    Rules:
      - Strips leading/trailing whitespace.
      - Raises InputRejected  if the result is empty.
      - Raises InputTooLong   if len(stripped) > max_length.

    Returns the stripped input on success.
    """
    stripped = text.strip() if text else ""
    if not stripped:
        raise InputRejected("Input must not be empty.", code="EMPTY_INPUT")
    if len(stripped) > max_length:
        raise InputTooLong(length=len(stripped), max_length=max_length)
    return stripped


def check_rate_limit(
    session_id: str,
    tool_name: str,
    max_per_session: int,
) -> None:
    """
    Raise RateLimitExceeded if *session_id* has already successfully executed
    *tool_name* at least *max_per_session* times.

    Reads agent_logs via crud — no new table required.

    Note: counts are based on the agent_log.selected_tool column, which records
    the primary tool for each run.  Multi-tool runs where a destructive action
    is not the first tool are conservatively under-counted.
    """
    from db import crud

    count = crud.count_successful_tool_calls(
        session_id=session_id,
        tool_name=tool_name,
    )
    if count >= max_per_session:
        raise RateLimitExceeded(
            tool_name=tool_name,
            count=count,
            max_per_session=max_per_session,
        )
