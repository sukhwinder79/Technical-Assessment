"""End-to-end wire-format test for the Groq / OpenAI-compatible path.

A stub HTTP server stands in for `https://api.groq.com/openai/v1`. The real
`OpenAICompatibleLLMClient`, the real `AgentExecutor` and the real tools all run,
so this asserts on the exact JSON the agent will put on the wire — tool schemas,
`assistant.tool_calls`, and `role: "tool"` result messages — which unit tests
with a stubbed SDK cannot check.

No API key and no network access required.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from agentic_sheets.agent.executor import AgentExecutor
from agentic_sheets.config import Settings
from agentic_sheets.events import EventBus
from agentic_sheets.llm.openai_compatible_client import OpenAICompatibleLLMClient
from agentic_sheets.memory import SessionMemory
from agentic_sheets.tools.registry import build_default_registry


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
        "usage": {"prompt_tokens": 900, "completion_tokens": 120, "total_tokens": 1020},
    }


class StubGroq(BaseHTTPRequestHandler):
    """Replays a scripted conversation and records every request body."""

    requests: list[dict] = []
    script: list[dict] = []

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("content-length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).requests.append(body)

        payload = type(self).script.pop(0) if type(self).script else _completion(content="done")
        encoded = json.dumps(payload).encode()

        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *_args):  # silence the default stderr logging
        return


@pytest.fixture
def stub_server():
    StubGroq.requests = []
    StubGroq.script = []
    server = HTTPServer(("127.0.0.1", 0), StubGroq)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        server.server_close()


def _settings(tmp_path: Path, base_url: str) -> Settings:
    return Settings(
        llm_provider="groq",
        groq_api_key="gsk_stub_key",
        llm_base_url=base_url,
        llm_model="llama-3.3-70b-versatile",
        agent_planning=False,
        agent_max_iterations=6,
        workspace_dir=tmp_path / "workspace",
        log_dir=tmp_path / "logs",
        memory_dir=tmp_path / "memory",
        tools_config=Path("config/tools.yaml").resolve(),
        google_auth_mode="disabled",
        tool_max_retries=0,
    )


def test_full_agent_turn_over_the_wire(tmp_path, stub_server):
    _server, base_url = stub_server
    settings = _settings(tmp_path, base_url)
    settings.ensure_directories()

    StubGroq.script = [
        _completion(
            content="I'll generate the CSV first.",
            tool_calls=[
                {
                    "id": "call_abc",
                    "type": "function",
                    "function": {
                        "name": "generate_employee_csv",
                        "arguments": json.dumps({"row_count": 21, "filename": "wire.csv", "seed": 3}),
                    },
                }
            ],
            finish_reason="tool_calls",
        ),
        _completion(content="Generated 21 rows. Excel and Sheets were not requested."),
    ]

    executor = AgentExecutor(
        llm=OpenAICompatibleLLMClient(settings),
        registry=build_default_registry(Path(settings.tools_config)),
        settings=settings,
        events=EventBus(),
        memory=SessionMemory("wire", Path(settings.memory_dir)),
    )

    result = executor.run("Create an employee CSV with 21 rows.")

    # --- the agent completed the real work -------------------------------
    assert result.status == "completed"
    assert result.steps[0].tool == "generate_employee_csv"
    assert result.steps[0].status == "succeeded"
    assert Path(result.artifacts["last_csv_path"]).exists()
    assert result.usage["input_tokens"] == 1800  # accumulated over both turns

    # --- request 1: system prompt + tools in OpenAI shape ----------------
    first = StubGroq.requests[0]
    assert first["model"] == "llama-3.3-70b-versatile"
    assert first["messages"][0]["role"] == "system"
    assert "autonomous spreadsheet operations agent" in first["messages"][0]["content"]
    assert first["messages"][1] == {"role": "user", "content": "Create an employee CSV with 21 rows."}

    # Claude-only parameters must never reach an OpenAI-compatible endpoint.
    assert "thinking" not in first
    assert "output_config" not in first
    assert "system" not in first

    tool_names = {tool["function"]["name"] for tool in first["tools"]}
    assert "generate_employee_csv" in tool_names
    assert "excel_import_csv" in tool_names
    generate = next(t for t in first["tools"] if t["function"]["name"] == "generate_employee_csv")
    assert generate["type"] == "function"
    assert generate["function"]["parameters"]["type"] == "object"
    assert "row_count" in generate["function"]["parameters"]["properties"]
    assert first["tool_choice"] == "auto"
    assert first["parallel_tool_calls"] is False

    # --- request 2: the assistant turn and tool result replayed correctly -
    second = StubGroq.requests[1]
    roles = [message["role"] for message in second["messages"]]
    assert roles == ["system", "user", "assistant", "tool"]

    assistant = second["messages"][2]
    assert assistant["content"] == "I'll generate the CSV first."
    assert assistant["tool_calls"][0]["id"] == "call_abc"
    assert assistant["tool_calls"][0]["type"] == "function"
    # Arguments must go back as a JSON *string*, not a nested object.
    assert isinstance(assistant["tool_calls"][0]["function"]["arguments"], str)
    assert json.loads(assistant["tool_calls"][0]["function"]["arguments"])["row_count"] == 21

    tool_message = second["messages"][3]
    assert tool_message["role"] == "tool"
    assert tool_message["tool_call_id"] == "call_abc"          # must match the call id
    payload = json.loads(tool_message["content"])
    assert payload["ok"] is True
    assert payload["row_count"] == 21


def test_small_model_operating_notes_are_included(tmp_path, stub_server):
    """The provider-aware prompt addendum only ships to non-Anthropic providers."""
    _server, base_url = stub_server
    settings = _settings(tmp_path, base_url)
    settings.ensure_directories()

    StubGroq.script = [_completion(content="Nothing to do.")]

    AgentExecutor(
        llm=OpenAICompatibleLLMClient(settings),
        registry=build_default_registry(Path(settings.tools_config)),
        settings=settings,
        events=EventBus(),
        memory=SessionMemory("notes", Path(settings.memory_dir)),
    ).run("hello")

    system = StubGroq.requests[0]["messages"][0]["content"]
    assert "Operating notes" in system
    assert "Call one tool per turn" in system


def test_a_tool_failure_is_reported_back_as_a_tool_message(tmp_path, stub_server):
    _server, base_url = stub_server
    settings = _settings(tmp_path, base_url)
    settings.ensure_directories()

    StubGroq.script = [
        _completion(
            tool_calls=[
                {
                    "id": "call_bad",
                    "type": "function",
                    "function": {
                        "name": "read_csv_preview",
                        "arguments": json.dumps({"csv_path": "does-not-exist.csv"}),
                    },
                }
            ],
            finish_reason="tool_calls",
        ),
        _completion(content="That file does not exist, so I stopped."),
    ]

    result = AgentExecutor(
        llm=OpenAICompatibleLLMClient(settings),
        registry=build_default_registry(Path(settings.tools_config)),
        settings=settings,
        events=EventBus(),
        memory=SessionMemory("fail", Path(settings.memory_dir)),
    ).run("preview a missing file")

    assert result.status == "failed"
    assert result.steps[0].status == "failed"

    tool_message = StubGroq.requests[1]["messages"][3]
    assert tool_message["role"] == "tool"
    payload = json.loads(tool_message["content"])
    assert payload["ok"] is False
    assert "remediation" in payload
