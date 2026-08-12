"""FastAPI surface: health, tools, a full synchronous run, and the SSE stream.

`build_agent` is patched with a fake so no API key or network is needed — what
is under test is the HTTP contract and the event plumbing between the worker
thread and the event loop.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi", reason="FastAPI is not installed.")

from fastapi.testclient import TestClient  # noqa: E402

from agentic_sheets.agent.executor import RunResult, StepRecord  # noqa: E402
from agentic_sheets.api import server as api_server  # noqa: E402


class FakeAgent:
    """Emits a realistic event sequence, then returns a realistic RunResult."""

    def __init__(self, events, status="completed") -> None:
        self.events = events
        self.status = status

    def run(
        self, instruction: str, *, continue_session: bool = False, run_id: str | None = None
    ) -> RunResult:
        self.events.emit(
            "run_started", "Run started", run_id=run_id or "fake", instruction=instruction
        )
        self.events.emit("tool_started", "generate_employee_csv(row_count=25)", tool="generate_employee_csv")
        self.events.emit("tool_succeeded", "25 rows → employees.csv", tool="generate_employee_csv")
        self.events.emit("run_finished", "done", status=self.status)
        return RunResult(
            run_id=run_id or "fake",
            session_id="fake-session",
            instruction=instruction,
            status=self.status,
            started_at=0.0,
            duration_s=1.5,
            final_message="CSV generated. Excel: SUCCESS. Google Sheets: SKIPPED.",
            steps=[
                StepRecord(
                    index=1,
                    tool="generate_employee_csv",
                    arguments={"row_count": 25},
                    status="succeeded",
                    duration_s=0.2,
                    summary="25 rows → employees.csv",
                )
            ],
            artifacts={"last_csv_path": "C:/work/employees.csv"},
            usage={"input_tokens": 1200, "output_tokens": 340},
            iterations=2,
        )


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("MEMORY_DIR", str(tmp_path / "memory"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    from agentic_sheets.config import get_settings

    get_settings(refresh=True)
    api_server.RUNS.clear()

    monkeypatch.setattr(
        api_server, "build_agent", lambda *, settings, events, session_id: FakeAgent(events)
    )
    with TestClient(api_server.app) as test_client:
        yield test_client


# ---- web UI ----------------------------------------------------------------


def test_root_serves_the_web_ui(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "no-store" in response.headers.get("cache-control", "")

    body = response.text
    assert "Agentic Spreadsheet Agent" in body
    # The UI must be self-contained: no CDN, no external fonts, no build step.
    for forbidden in ("http://cdn", "https://cdn", "unpkg.com", "jsdelivr", "fonts.googleapis"):
        assert forbidden not in body
    # It drives the same endpoints the CLI uses.
    assert "/runs/" in body and "EventSource" in body and "/health" in body


def test_ui_subscribes_to_every_event_type_the_agent_emits(client):
    """A new event type must not silently go unrendered in the browser."""
    from agentic_sheets.events import EventType

    body = client.get("/").text
    for event_type in EventType.__args__:
        assert f'"{event_type}"' in body, f"UI does not handle event type {event_type!r}"


def test_static_assets_are_served(client):
    assert client.get("/static/index.html").status_code == 200


# ---- read-only endpoints ---------------------------------------------------


def test_health_reports_the_environment(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["api_key_configured"] is True
    assert "excel_com_available" in body
    assert "google_auth_mode" in body


def test_tools_endpoint_publishes_schemas(client):
    body = client.get("/tools").json()
    names = {tool["name"] for tool in body["enabled"]}
    assert "generate_employee_csv" in names
    assert "excel_import_csv" in names
    generate = next(t for t in body["enabled"] if t["name"] == "generate_employee_csv")
    assert generate["input_schema"]["type"] == "object"
    assert "row_count" in generate["input_schema"]["properties"]


# ---- runs ------------------------------------------------------------------


def test_sync_run_returns_the_full_report(client):
    response = client.post("/runs/sync", json={"instruction": "Create an employee CSV"})
    assert response.status_code == 200

    body = response.json()
    assert body["status"] == "completed"
    result = body["result"]
    assert result["final_message"].startswith("CSV generated")
    assert result["steps"][0]["tool"] == "generate_employee_csv"
    assert result["artifacts"]["last_csv_path"].endswith("employees.csv")


def test_partial_run_maps_to_207(client, monkeypatch):
    monkeypatch.setattr(
        api_server,
        "build_agent",
        lambda *, settings, events, session_id: FakeAgent(events, status="partial"),
    )
    response = client.post("/runs/sync", json={"instruction": "both destinations"})
    assert response.status_code == 207
    assert response.json()["status"] == "partial"


def test_async_run_is_accepted_then_pollable(client):
    accepted = client.post("/runs", json={"instruction": "Create an employee CSV"})
    assert accepted.status_code == 202

    body = accepted.json()
    run_id = body["run_id"]
    assert body["events_url"] == f"/runs/{run_id}/events"

    # The SSE stream terminates once the run finishes, so draining it is also
    # how we wait for completion without sleeping.
    with client.stream("GET", f"/runs/{run_id}/events") as stream:
        types_seen = [
            json.loads(line[6:])["type"]
            for line in (raw.strip() for raw in stream.iter_lines())
            if line.startswith("data: ") and line[6:].startswith("{")
        ]

    assert "run_started" in types_seen
    assert "tool_succeeded" in types_seen

    finished = client.get(f"/runs/{run_id}").json()
    assert finished["status"] == "completed"
    assert finished["result"]["steps"][0]["status"] == "succeeded"


def test_runs_can_be_listed(client):
    client.post("/runs/sync", json={"instruction": "one"})
    body = client.get("/runs").json()
    assert len(body["runs"]) == 1
    assert body["runs"][0]["instruction"] == "one"


def test_unknown_run_is_404(client):
    assert client.get("/runs/deadbeef").status_code == 404
    assert client.get("/runs/deadbeef/events").status_code == 404


def test_a_misconfigured_agent_surfaces_as_a_failed_run(client, monkeypatch):
    from agentic_sheets.errors import ConfigurationError

    def explode(**_kwargs):
        raise ConfigurationError("ANTHROPIC_API_KEY is not set")

    monkeypatch.setattr(api_server, "build_agent", explode)

    response = client.post("/runs/sync", json={"instruction": "go"})
    assert response.status_code == 500
    body = response.json()
    assert body["status"] == "failed"
    assert "ANTHROPIC_API_KEY" in body["error"]


def test_default_instruction_matches_the_assessment_prompt(client):
    response = client.post("/runs/sync", json={})
    assert response.status_code == 200
    instruction = response.json()["result"]["instruction"]
    assert "Excel" in instruction and "Google Sheets" in instruction
