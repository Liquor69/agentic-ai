# Project: Hybrid AI Operations Agent

## Goal

Build a production-pattern, domain-agnostic AI agent system for small business operations automation. The system accepts a natural language business request, classifies it, selects the appropriate tool, executes an action via API or webhook, and returns a structured result with a full decision trace.

The primary purpose is a freelance portfolio demo. The architecture must visibly signal extensibility — a buyer should be able to read the repo structure and immediately understand that adding a new tool or workflow requires only adding a file to `tools/` and registering it, with zero changes to core logic.

The default tool configuration is customer operations (refund processing, subscription management) because the underlying logic already exists. This is one instantiation of the general architecture, not the system's identity.

---

## Architecture Invariant

Every request follows this loop without exception:

```
perceive(input + context + history)
→ plan(classify intent, select tool, validate against constraints)
→ act(execute tool call)
→ observe(result)
→ log(input / decision / action / result)
→ respond(result + trace)
```

The core loop is implemented in `core/agent.py` and `core/planner.py`. No other module should contain routing or decision logic.

---

## File Structure

```
project/
│
├── main.py                  # FastAPI app entry, router registration
├── config.py                # Env vars, model selection, constants
│
├── core/
│   ├── agent.py             # Main agent loop: perceive → plan → act → observe → log
│   ├── planner.py           # Tool selection logic, LangGraph state machine
│   ├── memory.py            # Conversation history, context window management
│   └── logger.py            # Structured logging: input / decision / action / result
│
├── tools/
│   ├── registry.py          # Tool registration and schema definitions (extensibility entry point)
│   ├── customer_ops.py      # Default config: refund processing, subscription actions
│   ├── webhooks.py          # Generic n8n / webhook dispatcher
│   └── external_api.py      # Generic HTTP tool wrapper for arbitrary external APIs
│
├── api/
│   ├── routes.py            # POST /run, GET /logs, GET /tools
│   └── schemas.py           # Pydantic request/response models
│
├── db/
│   ├── models.py            # SQLite table definitions (swap to PostgreSQL for production)
│   └── crud.py              # Log persistence, session storage
│
├── frontend/
│   ├── index.html           # Single-page UI: input field + decision trace display
│   └── app.js               # Fetch calls to /run, render structured result and trace
│
├── tests/
│   ├── test_agent.py        # Core routing logic unit tests
│   └── test_tools.py        # Tool execution tests
│
├── .env.example
├── requirements.txt
├── CLAUDE.md
└── README.md
```

---

## Technology Stack

- **Backend:** Python 3.14, FastAPI
- **Agent framework:** LangGraph (state machine for planner)
- **LLM:** Anthropic Claude API (Sonnet for reasoning, Haiku for classification/routing)
- **LLM client:** Abstracted in `config.py` — provider-swappable via `LLM_PROVIDER` env var
- **Database:** SQLite (default), PostgreSQL-compatible via SQLAlchemy
- **Frontend:** Vanilla HTML/JS — no framework
- **External integrations:** n8n webhooks (`webhooks.py`), generic HTTP (`external_api.py`)

---

## LLM Client Abstraction

`config.py` exposes a unified `llm_call(messages, system, tools, model_tier)` interface.
`model_tier` accepts `"fast"` (Haiku) or `"standard"` (Sonnet).
No file outside `config.py` references a provider SDK directly.

Caching strategy:
- System prompts and stable prior turns are marked `cache_control=ephemeral`
- Current user message is never cached (always dynamic)
- Cached blocks always precede dynamic content (strict prefix ordering)
- Cache write surcharge (25%) accepted — breakeven at 2 calls per session

---

## Tool Registry Pattern

`tools/registry.py` is the extensibility entry point. Adding a tool:

```python
@register_tool
def my_tool(input: MyToolInput) -> MyToolOutput:
    """One-line description used by the planner and GET /tools."""
    ...
```

The registry exposes all tools to the planner as a schema list. The planner never imports tools directly — only the registry. `discover_tools()` is called once at startup and imports all files in `tools/` automatically.

---

## API Contract

### `POST /run`
```json
Request:  { "message": str, "session_id": str | null, "allow_history_reference": bool }
Response: { "result": str, "trace": [...], "session_id": str, "iterations_used": int, "halt_reason": str | null }
```

### `GET /logs`
Query filters: `session_id`, `intent`, `selected_tool`, `has_error`, `since`, `until`, `limit`, `offset`

### `GET /tools`
Returns `[{"name": str, "description": str}]` — names and descriptions only, no schemas.

---

## LangGraph State Object

| Field | Type | Description |
|---|---|---|
| `input` | str | Raw user message |
| `intent` | str | Classified intent |
| `selected_tool` | str | Planner output |
| `available_tools` | list | Registered tool schemas at planning time |
| `selection_justification` | str | Planner reasoning |
| `expected_result` | str | Planner predicted output |
| `constraint_validation` | dict | Constraints checked and cleared |
| `tool_result` | any | Act node output |
| `error` | dict\|null | Structured error if any |
| `iterations` | int | Iteration counter |
| `session_id` | str | Session identifier |
| `history` | list | Conversation turns |

---

## Halting Conditions

| Condition | Behaviour |
|---|---|
| Success | Return result + trace |
| Tool execution failure | Return structured error + trace |
| Max iterations (5) | Escalate with reason |
| Ambiguity | Cycle back to perceive, ask clarifying question |

---

## Constraints

- Core loop logic lives exclusively in `core/` — no routing or decision logic elsewhere
- All policy constraints for `customer_ops.py` are encoded in the tool layer, not the planner
- The planner is stateless — all state passes through LangGraph state object explicitly
- No hardcoded API keys — all via `.env`
- Error handling is explicit at the tool execution layer with structured error responses
- `core/agent.py` and `core/planner.py` require explicit confirmation of state machine topology and halting conditions before implementation

---

## Auth

Static API key via `.env` — `X-API-Key` header check as FastAPI dependency.
Leave `API_KEY` empty to disable (development). Session ID is user-supplied; no ownership validation at demo stage.

---

## Database Schema

Three tables: `agent_logs`, `sessions`, `pending_confirmations`.

`agent_logs` fields: `session_id`, `timestamp`, `input`, `intent`, `selected_tool`, `reasoning`, `available_tools`, `selection_justification`, `expected_result`, `constraint_validation`, `tool_result`, `final_response`, `error`, `iterations_used`, `halt_reason`.

`pending_confirmations`: stores suspended action confirmations with 10-minute TTL. A new inbound message while confirmation is pending cancels the pending action.

---

## Deferred (require separate specification)

- `core/planner.py` — state machine topology, node definitions, routing logic
- `core/agent.py` — loop orchestration, node wiring
- `tools/webhooks.py` — n8n integration config
- `tools/external_api.py` — generic HTTP integration config
- `tests/test_agent.py` — depends on core logic
