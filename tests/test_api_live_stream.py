"""End-to-end HTTP run: real agent, real tools, stub LLM.

This is the contract the web UI depends on. Everything is real except the LLM,
which is a stub HTTP server speaking the OpenAI/Groq wire format:

    POST /runs            ->  202 + run_id
    GET  /runs/{id}/events ->  named SSE events, terminated by `done`
    GET  /runs/{id}        ->  the report the UI renders

A regression in the event names or the report shape breaks the browser silently,
which is exactly what this catches.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="FastAPI is not installed.")

from fastapi.testclient import TestClient  # noqa: E402

from agentic_sheets.api import server as api_server  # noqa: E402
from agentic_sheets.config import get_settings  # noqa: E402


def _completion(*, content=None, tool_calls=None, finish_reason="stop") -> dict:
    return {
        "id": "chatcmpl-stub",
        "object": "chat.completion",
        "created": 0,
        "model": "llama-3.3-70b-versatile",
        "choices": [
            {
                "index": 0,
                "finish_reason": finish_reason,
                "message": {"role": "assistant", "content": content, "tool_calls": tool_calls},
            }
        ],
        "usage": {"prompt_tokens": 800, "completion_tokens": 90, "total_tokens": 890},
    }


class StubLLM(BaseHTTPRequestHandler):
    script: list[dict] = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("content-length", 0))
        self.rfile.read(length)
        payload = type(self).script.pop(0) if type(self).script else _completion(content="done")
        encoded = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *_args):
        return


@pytest.fixture
def stub_llm():
    StubLLM.script = []
    server = HTTPServer(("127.0.0.1", 0), StubLLM)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def client(monkeypatch, tmp_path, stub_llm):
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_stub")
    monkeypatch.setenv("LLM_BASE_URL", stub_llm)
    monkeypatch.setenv("LLM_MODEL", "llama-3.3-70b-versatile")
    monkeypatch.setenv("AGENT_PLANNING", "false")
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("MEMORY_DIR", str(tmp_path / "memory"))
    monkeypatch.setenv("TOOLS_CONFIG", str(Path("config/tools.yaml").resolve()))
    monkeypatch.setenv("GOOGLE_AUTH_MODE", "disabled")

    get_settings(refresh=True)
    api_server.RUNS.clear()
    with TestClient(api_server.app) as test_client:
        yield test_client
    get_settings(refresh=True)


def read_sse(response) -> list[tuple[str, str]]:
    """Collect (event_name, data) pairs until the stream closes."""
    events: list[tuple[str, str]] = []
    name = None
    for raw in response.iter_lines():
        line = raw.strip() if isinstance(raw, str) else raw.decode().strip()
        if line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:"):
            events.append((name or "message", line[5:].strip()))
            name = None
    return events


def test_a_real_run_streams_the_events_the_ui_renders(client, tmp_path):
    StubLLM.script = [
        _completion(
            content="Generating the CSV now.",
            tool_calls=[
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "generate_employee_csv",
                        "arguments": json.dumps({"row_count": 24, "filename": "ui.csv", "seed": 8}),
                    },
                }
            ],
            finish_reason="tool_calls",
        ),
        _completion(content="- Generate CSV — **SUCCESS** — 24 rows in `ui.csv`."),
    ]

    accepted = client.post("/runs", json={"instruction": "Create an employee CSV with 24 rows."})
    assert accepted.status_code == 202
    run_id = accepted.json()["run_id"]

    with client.stream("GET", f"/runs/{run_id}/events") as stream:
        events = read_sse(stream)

    names = [name for name, _ in events]

    # The UI keys off these names; `done` is its signal to fetch the report.
    assert names[0] == "run_started"
    assert names[-1] == "done"
    assert "tool_started" in names
    assert "tool_succeeded" in names
    assert "run_finished" in names

    # Every payload except `done` is a JSON envelope with type/message/data.
    for name, data in events[:-1]:
        payload = json.loads(data)
        assert payload["type"] == name
        assert "message" in payload and "data" in payload

    started = json.loads(next(data for name, data in events if name == "tool_started"))
    assert started["data"]["tool"] == "generate_employee_csv"

    # The id in the URL, the events and the report must be the same id.
    run_started = json.loads(next(data for name, data in events if name == "run_started"))
    assert run_started["data"]["run_id"] == run_id

    # --- the report the UI renders ---------------------------------------
    report = client.get(f"/runs/{run_id}").json()
    assert report["status"] == "completed"

    result = report["result"]
    assert result["steps"][0]["tool"] == "generate_employee_csv"
    assert result["steps"][0]["status"] == "succeeded"
    assert result["steps"][0]["duration_s"] >= 0
    assert "SUCCESS" in result["final_message"]
    assert result["artifacts"]["last_csv_path"].endswith("ui.csv")
    assert result["usage"]["input_tokens"] == 1600
    assert result["iterations"] == 2

    # And the work actually happened on disk.
    assert Path(result["artifacts"]["last_csv_path"]).exists()


def test_a_late_subscriber_still_receives_the_whole_run(client):
    """The UI can attach after a run starts and must not miss the beginning."""
    StubLLM.script = [_completion(content="Nothing to do.")]

    run_id = client.post("/runs/sync", json={"instruction": "hello"}).json()["run_id"]

    # Subscribing after completion replays history, then closes.
    with client.stream("GET", f"/runs/{run_id}/events") as stream:
        names = [name for name, _ in read_sse(stream)]

    assert names[0] == "run_started"
    assert names[-1] == "done"


def test_a_failed_tool_reaches_the_ui_as_a_tool_failed_event(client):
    StubLLM.script = [
        _completion(
            tool_calls=[
                {
                    "id": "call_bad",
                    "type": "function",
                    "function": {
                        "name": "read_csv_preview",
                        "arguments": json.dumps({"csv_path": "missing.csv"}),
                    },
                }
            ],
            finish_reason="tool_calls",
        ),
        _completion(content="That file is missing."),
    ]

    run_id = client.post("/runs", json={"instruction": "preview a missing file"}).json()["run_id"]

    with client.stream("GET", f"/runs/{run_id}/events") as stream:
        events = read_sse(stream)

    failed = [json.loads(data) for name, data in events if name == "tool_failed"]
    assert failed and "not found" in failed[0]["message"].lower()

    report = client.get(f"/runs/{run_id}").json()
    assert report["status"] == "failed"
    assert report["result"]["steps"][0]["remediation"]
