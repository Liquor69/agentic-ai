# Hybrid AI Operations Agent

A production-pattern AI agent system for small business operations automation.

The system accepts a natural language request, classifies intent, selects the correct tool, executes an action, and returns a structured result with a full decision trace.

**This is a domain-agnostic architecture.** The default tool configuration handles gym membership operations (refunds, cancellations, pauses, plan changes) because the policy logic already existed. The architecture is not tied to this domain — adding a new tool domain requires one file and zero changes to core logic.

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

Open `http://localhost:8000` for the UI, or `http://localhost:8000/docs` for the API.

---

## How to add a new tool domain

The registry is the only integration point. No other file changes.

**1. Create a file in `tools/`:**

```python
# tools/billing_ops.py

from pydantic import BaseModel, Field
from tools.registry import register_tool

class InvoiceInput(BaseModel):
    account_id: str = Field(description="Account to invoice.")
    amount: float

class InvoiceOutput(BaseModel):
    success: bool
    invoice_id: str | None = None

@register_tool
def create_invoice(input: InvoiceInput) -> InvoiceOutput:
    """Create an invoice for an account."""
    # your logic here
    return InvoiceOutput(success=True, invoice_id="INV-001")
```

**2. That's it.**

`discover_tools()` runs at startup and imports every file in `tools/`. Your tool is immediately available to the planner, shows up in `GET /tools`, and can be selected for any matching request.

The planner never imports tool modules directly — it reads only the registry schema list. Adding `billing_ops.py` does not touch `core/`, `api/`, or any existing file.

---

## Architecture

Every request follows this loop without exception:

```
perceive(input + context + history)
→ plan(classify intent, select tool, validate constraints)
→ act(execute tool)
→ observe(result)
→ log(full trace to DB)
→ respond(result + trace)
```

```
project/
├── core/
│   ├── agent.py      # loop orchestration
│   ├── planner.py    # LangGraph state machine — tool selection
│   ├── memory.py     # session history
│   └── logger.py     # structured run logging
├── tools/
│   ├── registry.py   # ← extensibility entry point
│   └── *.py          # one file per tool domain
├── api/              # FastAPI routes + Pydantic schemas
├── db/               # SQLAlchemy models + CRUD
└── frontend/         # single-page UI
```

Core loop logic lives exclusively in `core/`. Tools enforce their own policy constraints. The planner is stateless — all state passes through the LangGraph state object.

---

## API

### `POST /run`
```json
{ "message": "I want a refund", "session_id": "abc", "allow_history_reference": true }
```
```json
{ "result": "...", "trace": [...], "session_id": "abc", "iterations_used": 1 }
```

### `GET /logs`
Query params: `session_id`, `intent`, `selected_tool`, `has_error`, `since`, `until`, `limit`, `offset`

### `GET /tools`
Returns all registered tool names and descriptions.

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
