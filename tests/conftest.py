"""Shared fixtures.

Everything here runs offline: no Anthropic key, no Excel, no Google
credentials. The LLM is replaced by a scripted fake so the agent loop itself is
under test rather than the model.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import pytest

from agentic_sheets.config import Settings
from agentic_sheets.events import EventBus
from agentic_sheets.llm.base import LLMResponse, ToolCall, ToolOutcome
from agentic_sheets.memory import SessionMemory
from agentic_sheets.tools.base import ToolContext


#: Every provider key and LLM knob that could leak in from the developer's shell.
_LEAKY_ENV_VARS = (
    "ANTHROPIC_API_KEY", "GROQ_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY",
    "TOGETHER_API_KEY", "LLM_API_KEY", "LLM_PROVIDER", "LLM_MODEL", "LLM_BASE_URL",
    "AGENT_MODEL", "AGENT_EFFORT", "AGENT_PLANNING", "GOOGLE_AUTH_MODE",
    "GOOGLE_CREDENTIALS_FILE", "GOOGLE_SHARE_WITH_EMAIL", "GOOGLE_SPREADSHEET_ID",
    "WORKSPACE_DIR", "LOG_DIR", "MEMORY_DIR", "TOOLS_CONFIG",
)


@pytest.fixture(autouse=True)
def hermetic_settings(monkeypatch):
    """Make the suite independent of the machine it runs on.

    Two sources of leakage, both of which caused real failures once a developer
    `.env` existed: pydantic-settings reads `.env` by default, and provider keys
    are often exported in the shell. Either one makes `pytest` pass locally and
    fail in CI (or vice versa), so both are severed here. Individual tests set
    exactly the environment they need on top of this.
    """
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for name in _LEAKY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        llm_provider="anthropic",
        anthropic_api_key="test-key-not-used",
        llm_model="claude-opus-5",
        agent_planning=False,
        agent_max_iterations=6,
        workspace_dir=tmp_path / "workspace",
        log_dir=tmp_path / "logs",
        memory_dir=tmp_path / "memory",
        tools_config=tmp_path / "tools.yaml",
        google_auth_mode="disabled",
        tool_max_retries=1,
        tool_retry_base_delay=0.0,
    )


@pytest.fixture
def memory(settings: Settings) -> SessionMemory:
    return SessionMemory("test-session", Path(settings.memory_dir))


@pytest.fixture
def events() -> EventBus:
    return EventBus()


@pytest.fixture
def ctx(settings: Settings, events: EventBus, memory: SessionMemory) -> ToolContext:
    workspace = Path(settings.workspace_dir)
    workspace.mkdir(parents=True, exist_ok=True)
    return ToolContext(settings=settings, events=events, memory=memory, workspace=workspace)


class FakeLLM:
    """Scripted `LLMClient`.

    `script` is a list of either LLMResponse objects or callables that receive
    the message history and return one — the latter lets a test assert that the
    model actually saw a tool result before deciding its next move.
    """

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.model = "fake-model"
        self.calls: list[dict[str, Any]] = []
        self.total_usage = {"input_tokens": 0, "output_tokens": 0}
        self.structured_payload: dict[str, Any] | None = None
        self.structured_calls = 0

    def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        on_text: Callable[[str], None] | None = None,
    ) -> LLMResponse:
        self.calls.append({"system": system, "messages": list(messages), "tools": tools})
        if not self.script:
            return LLMResponse(text="done", stop_reason="end_turn", raw_content=[{"type": "text", "text": "done"}])
        item = self.script.pop(0)
        response = item(messages) if callable(item) else item
        if on_text and response.text:
            on_text(response.text)
        return response

    def structured(self, *, system: str, prompt: str, schema: dict, max_tokens: int = 4096) -> dict:
        self.structured_calls += 1
        if self.structured_payload is None:
            raise RuntimeError("structured() not configured for this test")
        return self.structured_payload

    def build_assistant_message(self, response: LLMResponse) -> dict[str, Any]:
        content = response.raw_content or [{"type": "text", "text": response.text}]
        return {"role": "assistant", "content": content}

    def build_tool_result_messages(self, outcomes: list[ToolOutcome]) -> list[dict[str, Any]]:
        return [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": outcome.call_id,
                        "content": outcome.content,
                        **({"is_error": True} if outcome.is_error else {}),
                    }
                    for outcome in outcomes
                ],
            }
        ]


def tool_turn(*calls: tuple[str, dict], text: str = "") -> LLMResponse:
    """Build an assistant turn that requests one or more tool calls."""
    tool_calls = [
        ToolCall(id=f"toolu_{index}", name=name, arguments=arguments)
        for index, (name, arguments) in enumerate(calls)
    ]
    raw: list[dict[str, Any]] = []
    if text:
        raw.append({"type": "text", "text": text})
    raw += [
        {"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments} for call in tool_calls
    ]
    return LLMResponse(text=text, tool_calls=tool_calls, stop_reason="tool_use", raw_content=raw)


def final_turn(text: str) -> LLMResponse:
    return LLMResponse(text=text, stop_reason="end_turn", raw_content=[{"type": "text", "text": text}])
