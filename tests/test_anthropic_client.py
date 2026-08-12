"""The Anthropic adapter.

The SDK's `Anthropic` class is swapped for a stub, so these tests cover our own
translation layer — response parsing, refusal handling, usage accumulation,
request construction and message round-tripping — without a network call.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import anthropic
import httpx
import pytest

from agentic_sheets.errors import ConfigurationError, LLMError, RefusalError
from agentic_sheets.llm.anthropic_client import AnthropicLLMClient
from agentic_sheets.llm.base import LLMResponse, ToolOutcome

_REQUEST = httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def sdk_error(cls, status: int, message: str):
    """Build a real SDK exception — its constructor needs a genuine httpx response."""
    return cls(message=message, response=httpx.Response(status, request=_REQUEST), body=None)


# ---- stubs -----------------------------------------------------------------


def text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def tool_block(id_: str, name: str, arguments: dict) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=id_, name=name, input=arguments)


def thinking_block() -> SimpleNamespace:
    return SimpleNamespace(type="thinking", thinking="", signature="sig-abc")


def message(content, *, stop_reason="end_turn", stop_details=None, usage=None) -> SimpleNamespace:
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        stop_details=stop_details,
        model="claude-opus-5",
        usage=usage or SimpleNamespace(input_tokens=100, output_tokens=20, cache_read_input_tokens=0),
    )


class FakeStream:
    def __init__(self, final, events=()) -> None:
        self._final = final
        self._events = list(events)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def __iter__(self):
        return iter(self._events)

    def get_final_message(self):
        return self._final


class FakeMessages:
    def __init__(self) -> None:
        self.stream_calls: list[dict] = []
        self.create_calls: list[dict] = []
        self.stream_result = None
        self.create_result = None
        self.create_error: Exception | None = None

    def stream(self, **kwargs):
        self.stream_calls.append(kwargs)
        if isinstance(self.stream_result, Exception):
            raise self.stream_result
        return self.stream_result

    def create(self, **kwargs):
        self.create_calls.append(kwargs)
        if self.create_error is not None:
            raise self.create_error
        return self.create_result


class FakeSDK:
    def __init__(self, **_kwargs) -> None:
        self.messages = FakeMessages()


@pytest.fixture
def client(settings, monkeypatch) -> AnthropicLLMClient:
    settings.anthropic_api_key = "sk-ant-test"
    monkeypatch.setattr(anthropic, "Anthropic", FakeSDK)
    return AnthropicLLMClient(settings)


# ---- construction ----------------------------------------------------------


def test_a_missing_api_key_is_a_configuration_error(settings, monkeypatch):
    settings.anthropic_api_key = None
    monkeypatch.setattr(anthropic, "Anthropic", FakeSDK)
    with pytest.raises(ConfigurationError) as exc:
        AnthropicLLMClient(settings)
    assert "ANTHROPIC_API_KEY" in str(exc.value)


# ---- request construction --------------------------------------------------


def test_request_uses_adaptive_thinking_and_caches_the_system_prompt(client):
    client._client.messages.stream_result = FakeStream(message([text_block("hi")]))
    client.complete(system="SYSTEM", messages=[{"role": "user", "content": "hi"}])

    kwargs = client._client.messages.stream_calls[0]
    assert kwargs["thinking"] == {"type": "adaptive"}
    assert kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert kwargs["system"][0]["text"] == "SYSTEM"
    # Sampling parameters are not supported on current models.
    assert "temperature" not in kwargs and "top_p" not in kwargs
    assert "budget_tokens" not in json.dumps(kwargs["thinking"])


def test_effort_is_sent_only_when_configured(client, settings):
    client._client.messages.stream_result = FakeStream(message([text_block("hi")]))
    client.complete(system="s", messages=[])
    assert "output_config" not in client._client.messages.stream_calls[0]

    settings.agent_effort = "medium"
    client._client.messages.stream_result = FakeStream(message([text_block("hi")]))
    client.complete(system="s", messages=[])
    assert client._client.messages.stream_calls[1]["output_config"] == {"effort": "medium"}


def test_tools_are_forwarded_only_when_present(client):
    client._client.messages.stream_result = FakeStream(message([text_block("hi")]))
    client.complete(system="s", messages=[], tools=[{"name": "t"}])
    assert client._client.messages.stream_calls[0]["tools"] == [{"name": "t"}]


# ---- response parsing ------------------------------------------------------


def test_text_and_tool_calls_are_extracted(client):
    client._client.messages.stream_result = FakeStream(
        message(
            [text_block("I'll generate the CSV."), tool_block("toolu_1", "generate_employee_csv", {"row_count": 25})],
            stop_reason="tool_use",
        )
    )

    response = client.complete(system="s", messages=[])

    assert response.text == "I'll generate the CSV."
    assert response.stop_reason == "tool_use"
    assert response.wants_tools
    assert response.tool_calls[0].name == "generate_employee_csv"
    assert response.tool_calls[0].arguments == {"row_count": 25}


def test_multiple_tool_calls_in_one_turn_are_all_returned(client):
    client._client.messages.stream_result = FakeStream(
        message(
            [tool_block("t1", "a", {}), tool_block("t2", "b", {})],
            stop_reason="tool_use",
        )
    )
    response = client.complete(system="s", messages=[])
    assert [call.name for call in response.tool_calls] == ["a", "b"]


def test_non_dict_tool_input_degrades_to_empty_arguments(client):
    client._client.messages.stream_result = FakeStream(
        message([tool_block("t1", "a", None)], stop_reason="tool_use")
    )
    assert client.complete(system="s", messages=[]).tool_calls[0].arguments == {}


def test_streamed_text_deltas_reach_the_callback(client):
    events = [
        SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type="text_delta", text="Hel")),
        SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type="text_delta", text="lo")),
        SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type="thinking_delta", thinking="ignored")),
        SimpleNamespace(type="message_stop"),
    ]
    client._client.messages.stream_result = FakeStream(message([text_block("Hello")]), events)

    chunks: list[str] = []
    client.complete(system="s", messages=[], on_text=chunks.append)

    assert "".join(chunks) == "Hello"


def test_usage_accumulates_across_turns(client):
    for _ in range(2):
        client._client.messages.stream_result = FakeStream(
            message([text_block("x")], usage=SimpleNamespace(input_tokens=10, output_tokens=3, cache_read_input_tokens=5))
        )
        client.complete(system="s", messages=[])

    assert client.total_usage["input_tokens"] == 20
    assert client.total_usage["output_tokens"] == 6
    assert client.total_usage["cache_read_input_tokens"] == 10


# ---- error paths -----------------------------------------------------------


def test_a_refusal_raises_with_the_category(client):
    client._client.messages.stream_result = FakeStream(
        message([], stop_reason="refusal", stop_details=SimpleNamespace(category="cyber", explanation="no"))
    )
    with pytest.raises(RefusalError) as exc:
        client.complete(system="s", messages=[])
    assert "cyber" in str(exc.value)


def test_a_refusal_without_details_still_raises(client):
    client._client.messages.stream_result = FakeStream(message([], stop_reason="refusal"))
    with pytest.raises(RefusalError):
        client.complete(system="s", messages=[])


def test_a_bad_model_name_is_a_configuration_error(client):
    client._client.messages.stream_result = sdk_error(anthropic.NotFoundError, 404, "model not found")
    with pytest.raises(ConfigurationError) as exc:
        client.complete(system="s", messages=[])
    assert "AGENT_MODEL" in str(exc.value)


def test_a_bad_api_key_is_a_configuration_error(client):
    client._client.messages.stream_result = sdk_error(anthropic.AuthenticationError, 401, "invalid key")
    with pytest.raises(ConfigurationError) as exc:
        client.complete(system="s", messages=[])
    assert "API key" in str(exc.value)


def test_a_server_error_is_an_llm_error(client):
    client._client.messages.stream_result = sdk_error(anthropic.InternalServerError, 500, "boom")
    with pytest.raises(LLMError):
        client.complete(system="s", messages=[])


def test_a_connection_failure_is_an_llm_error(client):
    client._client.messages.stream_result = anthropic.APIConnectionError(request=_REQUEST)
    with pytest.raises(LLMError):
        client.complete(system="s", messages=[])


# ---- structured output (planner) -------------------------------------------


PLAN = {"goal": "g", "steps": [], "risks": []}


def test_structured_sends_a_json_schema_and_parses_the_result(client):
    client._client.messages.create_result = message([text_block(json.dumps(PLAN))])
    schema = {"type": "object", "properties": {}}

    assert client.structured(system="s", prompt="p", schema=schema) == PLAN
    assert client._client.messages.create_calls[0]["output_config"]["format"] == {
        "type": "json_schema",
        "schema": schema,
    }


def test_structured_falls_back_to_prompted_json_when_unsupported(client):
    calls = {"n": 0}
    original_create = client._client.messages.create

    def create(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sdk_error(anthropic.BadRequestError, 400, "output_config.format unsupported")
        return message([text_block("```json\n" + json.dumps(PLAN) + "\n```")])

    client._client.messages.create = create
    try:
        assert client.structured(system="s", prompt="p", schema={"type": "object"}) == PLAN
        assert calls["n"] == 2  # first attempt, then the fallback
    finally:
        client._client.messages.create = original_create


def test_structured_raises_on_unparseable_json(client):
    client._client.messages.create_result = message([text_block("not json at all")])
    with pytest.raises(LLMError):
        client.structured(system="s", prompt="p", schema={"type": "object"})


# ---- message construction --------------------------------------------------


def test_assistant_content_is_echoed_verbatim(client):
    """Thinking blocks carry signatures the API validates — never rebuild them."""
    blocks = [thinking_block(), text_block("hi")]
    response = LLMResponse(text="hi", raw_content=blocks)
    built = client.build_assistant_message(response)
    assert built == {"role": "assistant", "content": blocks}
    assert built["content"][0] is blocks[0]


def test_all_tool_results_go_back_in_one_user_message(client):
    messages = client.build_tool_result_messages(
        [
            ToolOutcome("t1", '{"ok": true}'),
            ToolOutcome("t2", '{"ok": false}', is_error=True),
        ]
    )
    assert len(messages) == 1  # Anthropic batches them; splitting suppresses parallel calls
    built = messages[0]
    assert built["role"] == "user"
    assert len(built["content"]) == 2
    assert "is_error" not in built["content"][0]
    assert built["content"][1]["is_error"] is True
    assert built["content"][1]["tool_use_id"] == "t2"
