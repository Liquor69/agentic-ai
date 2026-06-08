# Hybrid AI Operations Agent

A production-pattern AI agent system for small business operations automation.

The system accepts a natural language request, classifies intent, selects the correct tool, executes an action, and returns a structured result with a full decision trace.

**This is a domain-agnostic architecture.** The default tool configuration handles gym membership operations (refunds, cancellations, pauses, plan changes) because the policy logic already existed. The architecture is not tied to this domain — switching domains requires one env-var change and zero core modifications.

---

## Setup

```bash
git clone <repo>
cd <repo>

python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS/Linux

pip install -r requirements.txt

cp .env.example .env
# Edit .env — add your ANTHROPIC_API_KEY at minimum

uvicorn main:app --reload
```

Open `http://localhost:8000` for the UI, or `http://localhost:8000/docs` for the interactive API docs.

---

## How to add a new tool domain

A **domain pack** bundles the tools and routing rules for one business vertical. Switching domains is one env-var change. Adding a new domain is two files.

**1. Create `domains/<name>.py`** declaring the pack:

```python
from domains.base import DomainPack

PACK: DomainPack = {
    "name": "billing_ops",
    "display_name": "Billing Operations",
    "description": "Invoice creation and payment management.",
    "tools_modules": ["tools.billing_ops"],
    "classification_context": "You are a billing operations assistant.",
    "selection_rules": "Use create_invoice for any billing request.",
}
```

**2. Create `tools/<name>.py`** with the tool implementations:

```python
from pydantic import BaseModel, Field
from tools.registry import register_tool

class InvoiceInput(BaseModel):
    account_id: str
    amount: float

@register_tool
def create_invoice(input: InvoiceInput) -> dict:
    """Create an invoice for an account."""
    return {"success": True, "invoice_id": "INV-001"}
```

**3. Set the env var:**

```
DOMAIN_PACK=billing_ops
```

**That's it.** `discover_tools()` imports only that domain's modules at startup. The planner's classification and routing prompts are built from the domain pack. Zero changes to `core/`, `api/`, or any existing file.

---

## Architecture

Every request follows this loop without exception:

```
perceive(input + context + history)
→ plan(classify intent, select tool, validate constraints)
→ safety(rate-limit check)
→ act(execute tool  —  or simulate if dry_run=true)
→ observe(result)
→ log(full trace to DB)
→ respond(result + trace)
```

```
project/
├── core/
│   ├── agent.py      # loop entry point, input validation
│   ├── planner.py    # LangGraph state machine (classify → select → safety → execute)
│   ├── safety.py     # validate_input(), check_rate_limit()
│   ├── memory.py     # session history
│   └── logger.py     # structured run logging
├── domains/
│   ├── base.py       # DomainPack TypedDict
│   ├── __init__.py   # load_domain_pack() factory
│   ├── customer_ops.py
│   └── it_helpdesk.py
├── tools/
│   ├── registry.py   # ← extensibility entry point
│   └── *.py          # one file per tool domain
├── api/              # FastAPI routes + Pydantic schemas
├── db/               # SQLAlchemy models + CRUD
├── evals/            # eval corpus + harness
└── frontend/         # single-page UI
```

Core loop logic lives exclusively in `core/`. Tools enforce their own policy constraints. The planner is stateless — all state passes through the LangGraph state object.

---

## API

### `POST /run`

```json
{
  "message": "I want a refund",
  "session_id": "abc",
  "allow_history_reference": true,
  "dry_run": false
}
```

```json
{
  "result": "Refunded €60.00 to original payment method. Expected credit: 3–10 business days.",
  "trace": [{"phase": "interpretation", "data": {...}}, ...],
  "session_id": "abc",
  "iterations_used": 1,
  "halt_reason": "success",
  "latency_ms": 843,
  "token_usage": {"input_tokens": 412, "output_tokens": 38, "cache_read_input_tokens": 891},
  "dry_run": false
}
```

Set `dry_run: true` to simulate execution without any side effects. The full classify → select → safety loop runs; `execution_node` returns what *would* have been called. Useful for testing routing.

### `GET /metrics`

Aggregated observability stats from `agent_logs`. Optional `since` / `until` ISO-8601 query params.

```json
{
  "period": {"since": null, "until": null},
  "total_runs": 142,
  "by_halt_reason": {"success": 98, "confirmation_pending": 21, "form_required": 14, "ambiguity": 9},
  "latency_ms": {"avg": 912.4, "p50": 874, "p95": 1843},
  "tokens": {
    "total_prompt": 58420,
    "total_completion": 4311,
    "total_cache_read": 121900,
    "total_cache_creation": 8730
  },
  "error_rate": 0.0
}
```

### `GET /health`

Liveness + readiness probe. Returns `200` always; `status` is `"ok"` or `"degraded"`.

```json
{
  "status": "ok",
  "db": "ok",
  "domain": "customer_ops",
  "tools_registered": 7,
  "version": "1.0.0"
}
```

### `GET /logs`

Query params: `session_id`, `intent`, `selected_tool`, `has_error`, `since`, `until`, `limit`, `offset`

Each log row includes: `latency_ms`, `prompt_tokens`, `completion_tokens`, `cache_read_tokens`, `cache_creation_tokens`.

### `GET /tools`

Returns all registered tool names and descriptions for the active domain.

### `GET /dashboard`

Same payload as `/metrics` plus a `recent_runs` preview (last 10 runs). Consumed by the frontend Dashboard tab.

---

## Observability

Every run persists a full `agent_logs` row containing:

| Column | Description |
|---|---|
| `session_id` | Conversation session |
| `intent` / `selected_tool` | Classification + routing outcome |
| `halt_reason` | `success` · `tool_failure` · `confirmation_pending` · `form_required` · `ambiguity` · `rate_limited` · `dry_run` · `input_rejected` |
| `latency_ms` | Wall-clock time for the run |
| `prompt_tokens` | LLM input tokens (all calls, accumulated) |
| `completion_tokens` | LLM output tokens |
| `cache_read_tokens` | Tokens served from Anthropic prompt cache |
| `cache_creation_tokens` | Tokens written to cache |
| `trace` | Full JSON trace of every loop phase |

Token counts are accumulated across **all** LLM calls within a single run (classification + selection + any tool-internal calls) using a thread-local accumulator in `config.py`. Latency is measured wall-clock from `run_agent()` entry to `graph.invoke()` return.

---

## Action safety

| Guard | Where | Behaviour |
|---|---|---|
| Input validation | `core/agent.py` (pre-graph) | Rejects empty input and messages over `MAX_INPUT_LENGTH` (default 2000 chars). `halt_reason="input_rejected"`. Skipped for confirmation responses (`confirmed=True/False`). |
| Rate limiting | `core/planner.safety_node` | Blocks destructive actions when a session exceeds `RATE_LIMIT_PER_SESSION` successful calls for that tool (default 20). `halt_reason="rate_limited"`. |
| Conflict detection | `core/planner.selection_node` | Rejects plans that include mutually exclusive actions (e.g. cancel + refund). Returns a clarifying question. |
| Confirmation gate | `tools/*.py` + `execution_node` | Destructive tools return `confirmation_required=True` on the first call; execution is suspended until the user sends `confirmed=true`. TTL: 10 min. |

---

## Auth

Set `API_KEY` in `.env`. All endpoints require `X-API-Key: <value>` header.
Leave `API_KEY` empty to disable auth (development only).

---

## Stack

| Layer | Technology |
|---|---|
| Backend | Python 3.11+, FastAPI |
| Agent | LangGraph state machine |
| LLM | Anthropic Claude (Haiku for routing, Sonnet for reasoning) |
| Database | SQLite default — swap to PostgreSQL via `DATABASE_URL` |
| Frontend | Vanilla HTML/JS |

Provider is swappable: set `LLM_PROVIDER=openai` in `.env` to route all LLM calls through OpenAI. No code changes required.

---

## Tests

```bash
pytest tests/          # 152 tests — unit + integration
python evals/run_evals.py          # 51 eval cases, mock mode (no API key needed)
python evals/run_evals.py --live   # live mode (hits real Anthropic API)
```

The eval harness scores halt-reason accuracy and policy-outcome accuracy against a YAML corpus (`evals/corpus.yaml`). Mock mode patches the LLM and verifies routing logic only; live mode measures end-to-end quality.
