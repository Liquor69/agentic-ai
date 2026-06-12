"""
tests/test_integration.py

End-to-end integration tests against the FastAPI application.

Uses TestClient (httpx-backed synchronous client) to exercise the full HTTP
request stack: routing → serialisation → agent loop → DB → response.

LLM calls are patched at both binding sites so tests never hit the real API:
  - core.planner.llm_call  (used by interpretation_node and selection_node)
  - config.llm_call        (the canonical source; also patched for completeness)

The autouse _test_db fixture from conftest.py redirects all DB operations to
a per-test SQLite file, so tests are fully isolated.

Sections:
  1. Infrastructure     — /health, /tools, /metrics, /logs
  2. POST /run          — happy path, session continuity, dry_run flag
  3. Input validation   — empty input, over-length input
  4. Auth               — X-API-Key enforcement
  5. Safety signals     — dry_run response shape, safety node pass-through
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


# ─── App fixture ─────────────────────────────────────────────────────────────
# Function-scoped so each test gets a fresh lifespan (discover_tools re-runs).
# The autouse _test_db fixture patches the DB before the lifespan fires.

@pytest.fixture()
def client():
    from main import app
    with TestClient(app) as c:
        yield c


# ─── LLM mock helpers ─────────────────────────────────────────────────────────
# Mirror the dual-patch pattern from test_agent.py.

def _select_response(tool_name: str, tool_input: dict | None = None) -> dict:
    """Build a mock execute_plan response selecting a single tool."""
    return {
        "stop_reason": "tool_use",
        "tool_use": [{
            "name":  "execute_plan",
            "input": {
                "tools": [{
                    "tool_name":     tool_name,
                    "tool_input":    tool_input or {},
                    "justification": f"Selected {tool_name}.",
                }],
            },
        }],
        "content": "",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


def _classify_response(label: str = "test intent") -> dict:
    return {
        "stop_reason": "end_turn",
        "content": label,
        "tool_use": [],
        "usage": {"input_tokens": 5, "output_tokens": 3},
    }


def _text_response(text: str) -> dict:
    return {
        "stop_reason": "end_turn",
        "content": text,
        "tool_use": [],
        "usage": {"input_tokens": 8, "output_tokens": 12},
    }


@contextmanager
def _llm_patch(*responses):
    """Patch both LLM binding sites with a shared mock consuming *responses* in order."""
    mock = MagicMock(side_effect=list(responses))
    with patch("core.planner.llm_call", mock), patch("config.llm_call", mock):
        yield mock


# ─── Section 1: Infrastructure endpoints ─────────────────────────────────────

class TestInfrastructure:
    def test_health_returns_200(self, client):
        r = client.get("/health")
        assert r.status_code == 200

    def test_health_shape(self, client):
        data = client.get("/health").json()
        assert "status" in data
        assert "db" in data
        assert "domain" in data
        assert "tools_registered" in data
        assert "version" in data

    def test_health_status_ok(self, client):
        data = client.get("/health").json()
        assert data["status"] == "ok"
        assert data["db"] == "ok"

    def test_health_domain_matches_settings(self, client):
        from config import settings
        data = client.get("/health").json()
        assert data["domain"] == settings.domain_pack

    def test_health_tools_registered_positive(self, client):
        data = client.get("/health").json()
        assert data["tools_registered"] > 0

    def test_tools_returns_list(self, client):
        r = client.get("/tools")
        assert r.status_code == 200
        tools = r.json()
        assert isinstance(tools, list)
        assert len(tools) > 0

    def test_tools_entries_have_name_and_description(self, client):
        tools = client.get("/tools").json()
        for t in tools:
            assert "name" in t
            assert "description" in t
            assert isinstance(t["name"], str)
            assert isinstance(t["description"], str)

    def test_metrics_returns_200(self, client):
        r = client.get("/metrics")
        assert r.status_code == 200

    def test_metrics_shape(self, client):
        data = client.get("/metrics").json()
        assert "total_runs" in data
        assert "by_halt_reason" in data
        assert "latency_ms" in data
        assert "tokens" in data
        assert "error_rate" in data

    def test_logs_returns_200(self, client):
        r = client.get("/logs")
        assert r.status_code == 200

    def test_logs_shape(self, client):
        data = client.get("/logs").json()
        assert "logs" in data
        assert "total" in data
        assert "limit" in data
        assert "offset" in data


# ─── Section 2: POST /run ─────────────────────────────────────────────────────

class TestRunEndpoint:
    # faq_lookup makes 3 LLM calls: classify → select → internal FAQ answer
    _FAQ_ANSWER = _text_response("We are open Monday–Friday 6am–10pm, weekends 8am–8pm.")

    def test_run_returns_run_response_shape(self, client):
        with _llm_patch(
            _classify_response("FAQ lookup"),
            _select_response("faq_lookup", {"question": "What are your hours?"}),
            self._FAQ_ANSWER,
        ):
            r = client.post("/run", json={"message": "What are your opening hours?"})
        assert r.status_code == 200
        data = r.json()
        assert "result" in data
        assert "trace" in data
        assert "session_id" in data
        assert "iterations_used" in data
        assert "halt_reason" in data

    def test_run_returns_session_id(self, client):
        with _llm_patch(
            _classify_response("FAQ"),
            _select_response("faq_lookup", {"question": "test"}),
            self._FAQ_ANSWER,
        ):
            data = client.post("/run", json={"message": "test question"}).json()
        assert data["session_id"]
        assert isinstance(data["session_id"], str)

    def test_run_respects_provided_session_id(self, client):
        sid = "integration-test-session-abc123"
        with _llm_patch(
            _classify_response("FAQ"),
            _select_response("faq_lookup", {"question": "test"}),
            self._FAQ_ANSWER,
        ):
            data = client.post("/run", json={"message": "test", "session_id": sid}).json()
        assert data["session_id"] == sid

    def test_run_includes_latency_ms(self, client):
        with _llm_patch(
            _classify_response("FAQ"),
            _select_response("faq_lookup", {"question": "test"}),
            self._FAQ_ANSWER,
        ):
            data = client.post("/run", json={"message": "test question"}).json()
        assert data["latency_ms"] >= 0

    def test_run_includes_token_usage(self, client):
        with _llm_patch(
            _classify_response("FAQ"),
            _select_response("faq_lookup", {"question": "test"}),
            self._FAQ_ANSWER,
        ):
            data = client.post("/run", json={"message": "test question"}).json()
        assert isinstance(data["token_usage"], dict)

    def test_run_trace_is_list(self, client):
        with _llm_patch(
            _classify_response("FAQ"),
            _select_response("faq_lookup", {"question": "test"}),
            self._FAQ_ANSWER,
        ):
            data = client.post("/run", json={"message": "test question"}).json()
        assert isinstance(data["trace"], list)
        assert len(data["trace"]) > 0

    def test_run_trace_entries_have_phase(self, client):
        with _llm_patch(
            _classify_response("FAQ"),
            _select_response("faq_lookup", {"question": "test"}),
            self._FAQ_ANSWER,
        ):
            data = client.post("/run", json={"message": "test question"}).json()
        for step in data["trace"]:
            assert "phase" in step
            assert "data" in step


# ─── Section 3: Input validation ─────────────────────────────────────────────

class TestInputValidation:
    def test_empty_message_rejected(self, client):
        r = client.post("/run", json={"message": ""})
        assert r.status_code == 200          # HTTP 200 — structured error response
        data = r.json()
        assert data["halt_reason"] == "input_rejected"

    def test_whitespace_only_message_rejected(self, client):
        r = client.post("/run", json={"message": "   "})
        assert r.status_code == 200
        data = r.json()
        assert data["halt_reason"] == "input_rejected"

    def test_over_length_message_rejected(self, client):
        long_msg = "x" * 2001
        r = client.post("/run", json={"message": long_msg})
        assert r.status_code == 200
        data = r.json()
        assert data["halt_reason"] == "input_rejected"
        assert data["error"]["code"] == "INPUT_TOO_LONG"

    def test_normal_length_message_accepted(self, client):
        msg = "x" * 2000        # exactly at the limit
        with _llm_patch(
            _classify_response("FAQ"),
            _select_response("faq_lookup", {"question": "x"}),
            _text_response("FAQ answer"),
        ):
            r = client.post("/run", json={"message": msg})
        assert r.status_code == 200
        assert r.json()["halt_reason"] != "input_rejected"

    def test_input_rejected_response_has_no_tokens(self, client):
        r = client.post("/run", json={"message": ""})
        data = r.json()
        assert data["halt_reason"] == "input_rejected"
        assert data.get("token_usage") in (None, {}, {"input_tokens": 0})


# ─── Section 4: Auth ──────────────────────────────────────────────────────────

class TestAuth:
    def test_no_api_key_configured_allows_all(self, client):
        """When API_KEY is empty (default), all requests pass without a header."""
        with _llm_patch(
            _classify_response("FAQ"),
            _select_response("faq_lookup", {"question": "test"}),
        ):
            r = client.post("/run", json={"message": "test"})
        assert r.status_code == 200

    def test_wrong_api_key_returns_401(self, client):
        """When API_KEY is set, a wrong header is rejected."""
        with patch("api.routes.settings") as mock_settings:
            mock_settings.api_key = "secret-key-xyz"
            r = client.post(
                "/run",
                json={"message": "test"},
                headers={"X-API-Key": "wrong-key"},
            )
        assert r.status_code == 401

    def test_correct_api_key_passes(self, client):
        """The correct API key header is accepted."""
        with patch("api.routes.settings") as mock_settings:
            mock_settings.api_key = "secret-key-xyz"
            with _llm_patch(
                _classify_response("FAQ"),
                _select_response("faq_lookup", {"question": "test"}),
                _text_response("FAQ answer"),
            ):
                r = client.post(
                    "/run",
                    json={"message": "test"},
                    headers={"X-API-Key": "secret-key-xyz"},
                )
        assert r.status_code == 200


# ─── Section 5: Safety signals ────────────────────────────────────────────────

class TestSafetySignals:
    def test_dry_run_flag_echoed_in_response(self, client):
        with _llm_patch(
            _classify_response("refund request"),
            _select_response("process_refund"),
        ):
            data = client.post(
                "/run",
                json={"message": "I want a refund", "dry_run": True},
            ).json()
        assert data["dry_run"] is True

    def test_dry_run_halt_reason_is_dry_run(self, client):
        with _llm_patch(
            _classify_response("refund request"),
            _select_response("process_refund"),
        ):
            data = client.post(
                "/run",
                json={"message": "I want a refund", "dry_run": True},
            ).json()
        assert data["halt_reason"] == "dry_run"

    def test_dry_run_result_mentions_would_have_executed(self, client):
        with _llm_patch(
            _classify_response("refund request"),
            _select_response("process_refund"),
        ):
            data = client.post(
                "/run",
                json={"message": "I want a refund", "dry_run": True},
            ).json()
        # Result should describe what would have been executed
        assert "dry run" in data["result"].lower() or "would have" in data["result"].lower()

    def test_dry_run_false_by_default(self, client):
        with _llm_patch(
            _classify_response("FAQ"),
            _select_response("faq_lookup", {"question": "test"}),
        ):
            data = client.post("/run", json={"message": "test question"}).json()
        assert data["dry_run"] is False

    def test_safety_node_trace_step_present_on_success_path(self, client):
        """A safety phase trace step appears on the selection → execution path."""
        with _llm_patch(
            _classify_response("FAQ"),
            _select_response("faq_lookup", {"question": "test"}),
            _text_response("FAQ answer"),
        ):
            data = client.post("/run", json={"message": "test question"}).json()
        phases = [step["phase"] for step in data["trace"]]
        assert "safety" in phases

    def test_health_version_is_string(self, client):
        data = client.get("/health").json()
        assert isinstance(data["version"], str)
        assert data["version"]
