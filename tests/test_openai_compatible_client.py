"""The OpenAI-compatible adapter (Groq, OpenAI, OpenRouter, Together, Ollama).

The `openai.OpenAI` class is stubbed, so these tests cover our translation
layer: tool-schema conversion, parsing tool-call argument strings (including the
malformed JSON weaker models sometimes emit), message shapes, and the fact that
Claude-only parameters are never sent.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import openai
import pytest

from agentic_sheets.config import Settings
from agentic_sheets.errors import ConfigurationError, LLMError, RefusalError
from agentic_sheets.llm import build_client
from agentic_sheets.llm.base import LLMResponse, ToolOutcome
from agentic_sheets.llm.openai_compatible_client import OpenAICompatibleLLMClient

_REQUEST = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")


def sdk_error(cls, status: int, message: str = "boom"):
    return cls(message=message, response=httpx.Response(status, request=_REQUEST), body=None)


# ---- stubs -----------------------------------------------------------------


def tool_call(id_: str, name: str, arguments) -> SimpleNamespace:
    return SimpleNamespace(
        id=id_,
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def completion(content=None, tool_calls=None, finish_reason="stop", model="llama-3.3-70b-versatile"):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason=finish_reason,
                message=SimpleNamespace(content=content, tool_calls=tool_calls),
            )
        ],
        model=model,
        usage=SimpleNamespace(prompt_tokens=120, completion_tokens=30),
    )


class FakeCompletions:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.result = None
        self.results: list = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.results:
            result = self.results.pop(0)
        else:
            result = self.result
        if isinstance(result, Exception):
            raise result
        return result


class FakeOpenAI:
    def __init__(self, **kwargs) -> None:
        self.init_kwargs = kwargs
        self.base_url = kwargs.get("base_url")
        self.chat = SimpleNamespace(completions=FakeCompletions())


@pytest.fixture
def groq_settings(tmp_path) -> Settings:
    return Settings(
        llm_provider="groq",
        groq_api_key="gsk_test_key_1234567890",
        workspace_dir=tmp_path / "workspace",
        log_dir=tmp_path / "logs",
        memory_dir=tmp_path / "memory",
        google_auth_mode="disabled",
        agent_max_tokens=4000,
    )


@pytest.fixture
def client(groq_settings, monkeypatch) -> OpenAICompatibleLLMClient:
    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)
    return OpenAICompatibleLLMClient(groq_settings)


def completions(client) -> FakeCompletions:
    return client._client.chat.completions


# ---- provider resolution ---------------------------------------------------


def test_groq_defaults_are_applied(groq_settings):
    assert groq_settings.resolved_provider() == "groq"
    assert groq_settings.resolved_llm_base_url() == "https://api.groq.com/openai/v1"
    assert groq_settings.resolved_llm_model() == "llama-3.3-70b-versatile"
    assert groq_settings.expected_key_env_var() == "GROQ_API_KEY"
    assert groq_settings.uses_anthropic() is False


class _FakeRateLimit(Exception):
    """Stands in for openai.APIStatusError with a provider body attached."""

    def __init__(self, message: str, code: str = "rate_limit_exceeded") -> None:
        super().__init__(message)
        self.body = {"error": {"message": message, "code": code}}


def _rate_limit_hint(model: str, message: str, tmp_path, max_tokens: int = 8000) -> str:
    settings = Settings(
        llm_provider="groq",
        groq_api_key="gsk_test",
        llm_model=model,
        agent_max_tokens=max_tokens,
        memory_dir=tmp_path,
    )
    client = OpenAICompatibleLLMClient(settings)
    return client._rate_limit_hint(_FakeRateLimit(message))


def test_rate_limit_advice_never_suggests_the_failing_model(tmp_path):
    """Telling someone to switch to the model they are already on is useless."""
    for model in ("openai/gpt-oss-120b", "llama-3.3-70b-versatile"):
        for message in (
            "Rate limit reached ... on tokens per day (TPD): Limit 100000",
            "Request too large ... on tokens per minute (TPM): Limit 8000",
        ):
            hint = _rate_limit_hint(model, message, tmp_path)
            assert f"--model {model}" not in hint, f"suggested the failing model {model}"
            # ...but it must still offer a real alternative.
            assert "--model " in hint


def test_a_per_minute_limit_points_at_max_tokens(tmp_path):
    """The dominant cause: providers reserve max_tokens against the TPM budget."""
    hint = _rate_limit_hint(
        "openai/gpt-oss-120b",
        "Request too large ... on tokens per minute (TPM): Limit 8000, Requested 10336",
        tmp_path,
        max_tokens=8000,
    )
    assert "per-MINUTE" in hint
    assert "AGENT_MAX_TOKENS" in hint
    assert "8000" in hint          # shows the value actually in effect
    assert "reserve" in hint.lower()


def test_a_per_day_limit_does_not_suggest_waiting_a_minute(tmp_path):
    hint = _rate_limit_hint(
        "llama-3.3-70b-versatile",
        "Rate limit reached ... on tokens per day (TPD): Limit 100000, Used 94313",
        tmp_path,
    )
    assert "DAILY" in hint
    assert "waiting a minute will not help" in hint
    assert "AGENT_MAX_TOKENS" not in hint     # irrelevant when the day is spent


def test_llama_8b_is_never_recommended(tmp_path):
    """It cannot emit a parsable tool call for this workload."""
    hint = _rate_limit_hint("openai/gpt-oss-120b", "tokens per day (TPD): Limit 100000", tmp_path)
    assert "llama-3.1-8b-instant" not in hint


def test_a_non_rate_limit_error_produces_no_hint(tmp_path):
    settings = Settings(llm_provider="groq", groq_api_key="gsk_test", memory_dir=tmp_path)
    client = OpenAICompatibleLLMClient(settings)
    other = _FakeRateLimit("model not found", code="model_not_found")
    assert client._rate_limit_hint(other) is None


def test_a_per_day_limit_is_detected_from_the_message_alone(tmp_path):
    """Regression: detection keyed only on the rendered string missed rejections
    that name the limit without repeating the error code."""
    hint = _rate_limit_hint(
        "llama-3.3-70b-versatile",
        "Rate limit reached on tokens per day (TPD): Limit 100000",
        tmp_path,
    )
    assert hint is not None and "DAILY" in hint


def test_the_suite_is_isolated_from_a_developer_dotenv(tmp_path):
    """Guards the `hermetic_settings` fixture.

    Without it, `pytest` reads a real `.env` and provider keys exported in the
    shell, so the suite passes on the author's machine and fails in CI.
    """
    assert Settings(memory_dir=tmp_path).resolved_llm_api_key() is None


def test_auto_prefers_the_free_provider(tmp_path):
    settings = Settings(
        llm_provider="auto",
        groq_api_key="gsk_x",
        anthropic_api_key="sk-ant-y",
        memory_dir=tmp_path,
    )
    assert settings.resolved_provider() == "groq"


def test_auto_falls_back_to_anthropic_when_only_that_key_exists(tmp_path):
    settings = Settings(llm_provider="auto", anthropic_api_key="sk-ant-y", memory_dir=tmp_path)
    assert settings.resolved_provider() == "anthropic"
    assert settings.resolved_llm_model() == "claude-opus-5"


def test_explicit_model_and_base_url_override_the_defaults(tmp_path):
    settings = Settings(
        llm_provider="groq",
        groq_api_key="gsk_x",
        llm_model="openai/gpt-oss-120b",
        llm_base_url="https://proxy.internal/v1",
        memory_dir=tmp_path,
    )
    assert settings.resolved_llm_model() == "openai/gpt-oss-120b"
    assert settings.resolved_llm_base_url() == "https://proxy.internal/v1"


def test_the_factory_picks_the_openai_compatible_adapter_for_groq(groq_settings, monkeypatch):
    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)
    assert isinstance(build_client(groq_settings), OpenAICompatibleLLMClient)


def test_a_missing_key_names_the_right_env_var_and_the_free_signup(groq_settings, monkeypatch):
    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)
    groq_settings.groq_api_key = None
    with pytest.raises(ConfigurationError) as exc:
        OpenAICompatibleLLMClient(groq_settings)
    assert "GROQ_API_KEY" in str(exc.value)
    assert "console.groq.com" in str(exc.value)


def test_the_client_is_pointed_at_the_provider_base_url(client):
    assert client._client.init_kwargs["base_url"] == "https://api.groq.com/openai/v1"
    assert client.model == "llama-3.3-70b-versatile"


# ---- request construction --------------------------------------------------


def test_system_prompt_becomes_the_first_message(client):
    completions(client).result = completion(content="hi")
    client.complete(system="SYSTEM PROMPT", messages=[{"role": "user", "content": "hi"}])

    sent = completions(client).calls[0]
    assert sent["messages"][0] == {"role": "system", "content": "SYSTEM PROMPT"}
    assert sent["messages"][1] == {"role": "user", "content": "hi"}


def test_claude_only_parameters_are_never_sent(client):
    """`thinking` and `output_config` are Anthropic-specific and 400 elsewhere."""
    completions(client).result = completion(content="hi")
    client.complete(system="s", messages=[])
    sent = completions(client).calls[0]
    assert "thinking" not in sent
    assert "output_config" not in sent
    assert "system" not in sent  # it went into messages instead


def test_tools_are_wrapped_in_the_function_envelope(client):
    completions(client).result = completion(content="hi")
    client.complete(
        system="s",
        messages=[],
        tools=[
            {
                "name": "generate_employee_csv",
                "description": "Generate a CSV.",
                "input_schema": {"type": "object", "properties": {"row_count": {"type": "integer"}}},
            }
        ],
    )

    sent = completions(client).calls[0]
    assert sent["tool_choice"] == "auto"
    assert sent["parallel_tool_calls"] is False  # small models fan out badly
    function = sent["tools"][0]
    assert function["type"] == "function"
    assert function["function"]["name"] == "generate_employee_csv"
    assert function["function"]["parameters"]["properties"]["row_count"]["type"] == "integer"


def test_parallel_tool_calls_can_be_re_enabled(client, groq_settings):
    groq_settings.llm_disable_parallel_tool_calls = False
    completions(client).result = completion(content="hi")
    client.complete(system="s", messages=[], tools=[{"name": "t", "description": "d", "input_schema": {}}])
    assert "parallel_tool_calls" not in completions(client).calls[0]


# ---- response parsing ------------------------------------------------------


def test_tool_calls_are_parsed_from_the_json_argument_string(client):
    completions(client).result = completion(
        content="Generating the CSV.",
        tool_calls=[tool_call("call_1", "generate_employee_csv", '{"row_count": 25, "seed": 7}')],
        finish_reason="tool_calls",
    )

    response = client.complete(system="s", messages=[])

    assert response.stop_reason == "tool_use"
    assert response.tool_calls[0].name == "generate_employee_csv"
    assert response.tool_calls[0].arguments == {"row_count": 25, "seed": 7}
    assert response.text == "Generating the CSV."


def test_malformed_tool_arguments_do_not_crash_the_run(client):
    """A weaker model emitting broken JSON must reach the tool's own validation."""
    completions(client).result = completion(
        tool_calls=[tool_call("call_1", "generate_employee_csv", '{"row_count": 25,,,}')],
        finish_reason="tool_calls",
    )

    response = client.complete(system="s", messages=[])

    assert response.tool_calls[0].arguments == {}
    assert response.tool_calls[0].name == "generate_employee_csv"


def test_non_object_tool_arguments_degrade_to_empty(client):
    completions(client).result = completion(
        tool_calls=[tool_call("call_1", "t", "[1, 2, 3]")], finish_reason="tool_calls"
    )
    assert client.complete(system="s", messages=[]).tool_calls[0].arguments == {}


def test_tool_calls_win_over_a_stop_finish_reason(client):
    """Some providers report finish_reason='stop' alongside tool calls."""
    completions(client).result = completion(
        tool_calls=[tool_call("call_1", "t", "{}")], finish_reason="stop"
    )
    assert client.complete(system="s", messages=[]).stop_reason == "tool_use"


@pytest.mark.parametrize(
    ("finish_reason", "expected"),
    [("stop", "end_turn"), ("length", "max_tokens"), ("tool_calls", "tool_use")],
)
def test_finish_reasons_map_to_normalised_stop_reasons(client, finish_reason, expected):
    calls = [tool_call("c", "t", "{}")] if finish_reason == "tool_calls" else None
    completions(client).result = completion(content="x", tool_calls=calls, finish_reason=finish_reason)
    assert client.complete(system="s", messages=[]).stop_reason == expected


def test_content_filter_raises_a_refusal(client):
    completions(client).result = completion(content=None, finish_reason="content_filter")
    with pytest.raises(RefusalError):
        client.complete(system="s", messages=[])


def test_streamed_text_callback_receives_the_message(client):
    completions(client).result = completion(content="Working on it.")
    chunks: list[str] = []
    client.complete(system="s", messages=[], on_text=chunks.append)
    assert chunks == ["Working on it."]


def test_usage_accumulates(client):
    for _ in range(2):
        completions(client).result = completion(content="x")
        client.complete(system="s", messages=[])
    assert client.total_usage == {"input_tokens": 240, "output_tokens": 60}


def test_an_empty_choices_list_is_an_llm_error(client):
    completions(client).result = SimpleNamespace(choices=[], model="m", usage=None)
    with pytest.raises(LLMError):
        client.complete(system="s", messages=[])


# ---- error translation -----------------------------------------------------


def test_a_bad_key_is_a_configuration_error(client):
    completions(client).result = sdk_error(openai.AuthenticationError, 401)
    with pytest.raises(ConfigurationError) as exc:
        client.complete(system="s", messages=[])
    assert "GROQ_API_KEY" in str(exc.value)


def test_a_decommissioned_model_points_at_the_provider_model_list(client):
    completions(client).result = sdk_error(openai.NotFoundError, 404, "model not found")
    with pytest.raises(ConfigurationError) as exc:
        client.complete(system="s", messages=[])
    assert "LLM_MODEL" in str(exc.value)
    assert "groq.com/docs/models" in str(exc.value)


def test_a_rate_limit_is_an_llm_error(client):
    completions(client).result = sdk_error(openai.RateLimitError, 429)
    with pytest.raises(LLMError):
        client.complete(system="s", messages=[])


# ---- structured output -----------------------------------------------------


PLAN = {"goal": "g", "steps": [{"id": 1}], "risks": []}


def test_structured_requests_a_json_object_and_parses_it(client):
    completions(client).result = completion(content=json.dumps(PLAN))
    assert client.structured(system="s", prompt="p", schema={"type": "object"}) == PLAN
    assert completions(client).calls[0]["response_format"] == {"type": "json_object"}


def test_structured_strips_markdown_fences(client):
    completions(client).result = completion(content=f"```json\n{json.dumps(PLAN)}\n```")
    assert client.structured(system="s", prompt="p", schema={}) == PLAN


def test_structured_recovers_json_wrapped_in_prose(client):
    completions(client).result = completion(content=f"Sure! Here is the plan:\n{json.dumps(PLAN)}\nHope that helps.")
    assert client.structured(system="s", prompt="p", schema={}) == PLAN


def test_structured_retries_without_response_format_when_unsupported(client):
    completions(client).results = [
        sdk_error(openai.BadRequestError, 400, "response_format not supported"),
        completion(content=json.dumps(PLAN)),
    ]
    assert client.structured(system="s", prompt="p", schema={}) == PLAN
    assert "response_format" not in completions(client).calls[1]


def test_structured_raises_on_unparseable_output(client):
    completions(client).result = completion(content="I cannot produce JSON.")
    with pytest.raises(LLMError):
        client.structured(system="s", prompt="p", schema={})


# ---- message construction ---------------------------------------------------


def test_assistant_message_serialises_tool_calls_back_to_json_strings(client):
    from agentic_sheets.llm.base import ToolCall

    response = LLMResponse(
        text="Calling the tool.",
        tool_calls=[ToolCall("call_1", "generate_employee_csv", {"row_count": 25})],
        stop_reason="tool_use",
    )
    built = client.build_assistant_message(response)

    assert built["role"] == "assistant"
    assert built["content"] == "Calling the tool."
    assert built["tool_calls"][0]["id"] == "call_1"
    assert json.loads(built["tool_calls"][0]["function"]["arguments"]) == {"row_count": 25}


def test_assistant_message_with_no_text_uses_null_content(client):
    from agentic_sheets.llm.base import ToolCall

    built = client.build_assistant_message(
        LLMResponse(text="", tool_calls=[ToolCall("c", "t", {})], stop_reason="tool_use")
    )
    assert built["content"] is None


def test_each_tool_result_is_its_own_tool_role_message(client):
    messages = client.build_tool_result_messages(
        [ToolOutcome("call_1", '{"ok": true}'), ToolOutcome("call_2", '{"ok": false}', is_error=True)]
    )
    assert len(messages) == 2  # unlike Anthropic, these must not be batched
    assert messages[0] == {"role": "tool", "tool_call_id": "call_1", "content": '{"ok": true}'}
    assert messages[1]["tool_call_id"] == "call_2"
