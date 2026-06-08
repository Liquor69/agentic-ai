# Architecture Decisions

## agent_logs schema

One row per agent run. Fields:

| Column | Type | Notes |
|---|---|---|
| session_id | TEXT | Groups rows by conversation |
| timestamp | DATETIME | UTC, set at log time |
| input | TEXT | Raw user message |
| intent | TEXT | Classified intent |
| selected_tool | TEXT | Tool chosen by planner |
| reasoning | TEXT | Planner free-text reasoning |
| available_tools | TEXT JSON | Tool names visible at planning time |
| selection_justification | TEXT | Why this tool was selected |
| expected_result | TEXT | Planner-predicted outcome |
| constraint_validation | TEXT JSON | Policy checks performed |
| tool_result | TEXT JSON | Raw tool output |
| final_response | TEXT | Formatted response sent to user |
| error | TEXT JSON | Structured error if any |
| iterations_used | INT | How many planning loops ran |
| halt_reason | TEXT | success / tool_failure / ambiguity / etc. |
| trace | TEXT JSON | Full per-phase trace list |

## API Contract

### POST /run
- Request: `{ "message": str, "session_id": str|null, "allow_history_reference": bool, "confirmed": bool|null, "form_data": dict|null, "account_id": str|null }`
- Response: `{ "result": str, "trace": [...], "session_id": str, "iterations_used": int, "halt_reason": str|null, "error": dict|null, "form_spec": dict|null }`

### GET /logs
- Query filters: `session_id`, `intent`, `selected_tool`, `has_error`, `since`, `until`, `limit`, `offset`

### GET /tools
- Returns `[{"name": str, "description": str}]` — names and descriptions only, no schemas.

### GET /accounts
- Returns the demo archetype list.

### POST /accounts/custom
- Sets the custom demo account.

## Session history extension point

`core/memory.py` exposes `build_context()` which currently loads only within-session history.
Cross-session retrieval (RAG-style) would be added here — no other module changes required.

## Halting conditions

| Condition | halt_reason | Behaviour |
|---|---|---|
| Tool executed successfully | success | Return result + trace |
| Tool returned ToolError | tool_failure | Return structured error + trace |
| Max iterations exceeded | max_iterations | Escalate with reason |
| LLM could not select tool | ambiguity | Ask clarifying question |
| Required params missing | form_required | Return form spec for frontend |
| Action needs YES/NO | confirmation_pending | Suspend; await confirmed=True/False |
| Member said NO | cancelled | Cancel pending, return message |
| Confirmation TTL expired | confirmation_expired | Inform user; resubmit required |
