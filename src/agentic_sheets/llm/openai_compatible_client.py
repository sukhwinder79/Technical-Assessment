"""LLM adapter for any OpenAI-compatible chat-completions API with tool calling.

One class covers several providers because they all speak the same wire format:

    Groq            https://api.groq.com/openai/v1        free tier, very fast
    OpenAI          https://api.openai.com/v1
    OpenRouter      https://openrouter.ai/api/v1          free models available
    Together AI     https://api.together.xyz/v1
    Ollama (local)  http://localhost:11434/v1             no key needed

Only the base URL, key and model name change — see `LLM_PROVIDER` in `.env.example`.

Differences from the Anthropic adapter that this class absorbs so the agent loop
never has to care:

* the system prompt is `messages[0]`, not a separate parameter;
* tools are wrapped in `{"type": "function", "function": {...}}`;
* tool arguments arrive as a JSON *string* that has to be parsed (and small
  models sometimes emit invalid JSON, which is handled rather than crashing);
* every tool result is its own `role: "tool"` message;
* there is no `thinking` / `effort` concept — those parameters are Claude-only
  and must not be sent.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from ..config import Settings
from ..errors import ConfigurationError, LLMError, RefusalError
from ..logging_setup import get_logger
from .base import LLMResponse, ToolCall, ToolOutcome

log = get_logger(__name__)

#: finish_reason -> our normalised stop_reason
_STOP_REASONS = {
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "stop": "end_turn",
    "length": "max_tokens",
    "content_filter": "refusal",
}

#: Groq returns this when its server-side parser cannot turn the model's
#: `<function=name {...}</function>` text into a structured tool call. It is
#: sampling variance, not a bad request from us, so it is worth retrying.
_TOOL_USE_FAILED = "tool_use_failed"

#: How many times to re-sample a turn that failed to produce a parsable call.
_TOOL_PARSE_RETRIES = 3

#: Models to offer when the current one runs out of budget. Verified to handle
#: multi-step tool calling; `llama-3.1-8b-instant` is deliberately absent
#: because it cannot emit a parsable tool call for this workload.
_SUGGESTED_MODELS = ("llama-3.3-70b-versatile", "openai/gpt-oss-120b")

#: JSON Schema keys that small open models handle reliably. Anything else is
#: dropped, because extra vocabulary measurably degrades their tool calls.
_KEPT_SCHEMA_KEYS = frozenset(
    {"type", "description", "enum", "properties", "required", "items", "additionalProperties"}
)


def simplify_schema(schema: Any) -> Any:
    """Flatten a Pydantic JSON Schema into the subset small models handle well.

    Pydantic renders `int | None` as
    ``{"anyOf": [{"type": "integer"}, {"type": "null"}], "default": None}``.
    Llama-class models frequently mis-generate against that union — in testing
    it produced ``<function=generate_employee_csv [{"seed": null, ...}]``, an
    array where an object belongs, which Groq then rejects with a 400
    ``tool_use_failed``. Collapsing the union to its one real type removes the
    trigger. `title` and `default` are dropped for the same reason: they are
    noise the model tries to interpret.

    Optionality is preserved via `required`, which is the part that actually
    matters — a field left out of `required` may simply be omitted.
    """
    if isinstance(schema, list):
        return [simplify_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema

    # Collapse nullable unions: anyOf/oneOf where exactly one branch is real.
    for union_key in ("anyOf", "oneOf"):
        if union_key in schema:
            branches = [
                branch
                for branch in schema[union_key]
                if not (isinstance(branch, dict) and branch.get("type") == "null")
            ]
            merged = dict(simplify_schema(branches[0])) if len(branches) == 1 else {}
            if description := schema.get("description"):
                merged.setdefault("description", description)
            # More than one real branch: fall back to a permissive string,
            # which the tool's own Pydantic validation will coerce.
            return merged or {"type": "string", "description": schema.get("description", "")}

    cleaned: dict[str, Any] = {}
    for key, value in schema.items():
        if key not in _KEPT_SCHEMA_KEYS:
            continue  # drops title, default, format, examples, $defs noise
        if key == "properties" and isinstance(value, dict):
            cleaned[key] = {name: simplify_schema(sub) for name, sub in value.items()}
        elif key in ("items",):
            cleaned[key] = simplify_schema(value)
        else:
            cleaned[key] = value

    # A tool with no arguments still needs a valid object schema.
    if cleaned.get("type") == "object":
        cleaned.setdefault("properties", {})
    return cleaned


class OpenAICompatibleLLMClient:
    """Chat-completions client for Groq and other OpenAI-compatible providers."""

    def __init__(self, settings: Settings) -> None:
        try:
            import openai
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise ConfigurationError(
                "The 'openai' package is required for OpenAI-compatible providers "
                "(including Groq). Run: pip install -r requirements.txt"
            ) from exc

        api_key = settings.resolved_llm_api_key()
        base_url = settings.resolved_llm_base_url()

        if not api_key:
            raise ConfigurationError(
                f"No API key found for LLM_PROVIDER={settings.llm_provider}. "
                f"Set {settings.expected_key_env_var()} in .env "
                "(Groq keys are free at https://console.groq.com/keys)."
            )

        self._openai = openai
        self._client = openai.OpenAI(api_key=api_key, base_url=base_url, max_retries=3)
        self.settings = settings
        self.provider = settings.llm_provider
        self.model = settings.resolved_llm_model()
        self.total_usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}

    # ---- helpers -----------------------------------------------------------

    def _accumulate_usage(self, usage: Any) -> dict[str, int]:
        snapshot = {
            "input_tokens": getattr(usage, "prompt_tokens", 0) or 0,
            "output_tokens": getattr(usage, "completion_tokens", 0) or 0,
        }
        for key, value in snapshot.items():
            self.total_usage[key] += value
        return snapshot

    @staticmethod
    def _to_openai_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool["description"],
                    "parameters": simplify_schema(tool["input_schema"]),
                },
            }
            for tool in tools
        ]

    @staticmethod
    def _is_tool_parse_failure(exc: Exception) -> bool:
        """Did the provider fail to parse the model's tool call?

        Groq surfaces this as a 400 with `code: "tool_use_failed"`. It is the
        model's sampling, not our request, so re-sampling usually fixes it.
        """
        body = getattr(exc, "body", None)
        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, dict) and error.get("code") == _TOOL_USE_FAILED:
                return True
        return _TOOL_USE_FAILED in str(exc)

    def _create_with_tool_parse_retry(self, payload: dict[str, Any]):
        """Send the turn, re-sampling if the provider couldn't parse a tool call."""
        last: Exception | None = None
        for attempt in range(1, _TOOL_PARSE_RETRIES + 1):
            try:
                return self._client.chat.completions.create(**payload)
            except Exception as exc:  # noqa: BLE001 - classified below
                if not self._is_tool_parse_failure(exc) or attempt == _TOOL_PARSE_RETRIES:
                    if self._is_tool_parse_failure(exc):
                        raise LLMError(
                            f"{self.model} produced a tool call {self.provider} could not parse, "
                            f"{_TOOL_PARSE_RETRIES} times in a row. This is a weakness of smaller "
                            "models rather than a bad request. Try a stronger model "
                            "(LLM_MODEL=openai/gpt-oss-120b) or LLM_PROVIDER=anthropic."
                        ) from exc
                    raise self._wrap_api_errors(exc) from exc
                last = exc
                log.warning(
                    "llm.tool_call.unparsable_retrying",
                    provider=self.provider,
                    model=self.model,
                    attempt=attempt,
                )
        raise self._wrap_api_errors(last) from last  # pragma: no cover - loop always returns/raises

    def _rate_limit_hint(self, exc: Exception) -> str | None:
        """Turn a provider token/rate-limit rejection into actionable advice.

        Groq's free tier enforces a tokens-per-minute budget, and it reports an
        over-budget request as a 413 with `code: rate_limit_exceeded`. Retrying
        cannot help when a *single* request exceeds the per-minute limit, so the
        useful response is to say what to change.
        """
        text = str(exc)
        detail = text
        code = ""
        body = getattr(exc, "body", None)
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            detail = str(body["error"].get("message", text))
            code = str(body["error"].get("code", ""))

        # Match on the structured code as well as the prose. Relying on the
        # rendered string alone missed per-day rejections whose message names
        # the limit ("tokens per day") without repeating the error code.
        haystack = f"{text} {detail} {code}".lower()
        if not any(
            marker in haystack
            for marker in ("rate_limit_exceeded", "tokens per minute", "tokens per day", "(tpm)", "(tpd)")
        ):
            return None

        # Per-DAY and per-MINUTE exhaustion need completely different responses,
        # so say which one it is rather than guessing on the user's behalf.
        per_day = "per day" in haystack or "(tpd)" in haystack

        # Never recommend the model that is already failing.
        alternatives = [m for m in _SUGGESTED_MODELS if m != self.model]
        model_hint = " or ".join(f"`--model {m}`" for m in alternatives[:2])

        if per_day:
            advice = (
                f"The DAILY token allowance for `{self.model}` is used up, so waiting a minute "
                "will not help. Options:\n"
                f"  • switch model — each is metered separately, so {model_hint} may still have "
                "budget left today;\n"
                "  • switch provider: `--provider anthropic` (or openai / openrouter / ollama);\n"
                "  • run `--provider ollama` for a fully local model with no quota at all;\n"
                "  • or wait for the reset time quoted above."
            )
        else:
            advice = (
                "This is the per-MINUTE token cap.\n\n"
                "Note that providers reserve `max_tokens` against this budget, so an "
                f"over-provisioned value is rejected before the request is even read — "
                f"AGENT_MAX_TOKENS is currently {self.settings.agent_max_tokens}. Options, "
                "cheapest first:\n"
                "  • lower AGENT_MAX_TOKENS in .env (2000 is plenty; the agent's longest "
                "output is its final report);\n"
                "  • add `--no-plan`, which removes a whole request from the run;\n"
                "  • wait a minute, then resume with `--session <name> --continue` — earlier "
                "steps are remembered, so nothing is redone;\n"
                f"  • switch model: {model_hint};\n"
                "  • narrow the toolbox in config/tools.yaml (fewer schemas per request);\n"
                "  • or switch provider, e.g. `--provider anthropic`."
            )

        return (
            f"{self.provider} rejected the request: its token allowance is exhausted.\n\n"
            f"Provider said: {detail}\n\n"
            f"{advice}"
        )

    def _wrap_api_errors(self, exc: Exception) -> Exception:
        openai = self._openai
        if isinstance(exc, openai.AuthenticationError):
            return ConfigurationError(
                f"{self.provider} rejected the API key. Check "
                f"{self.settings.expected_key_env_var()} in .env. ({exc})"
            )
        if isinstance(exc, openai.NotFoundError):
            return ConfigurationError(
                f"Model '{self.model}' was not found on {self.provider}. "
                "Set LLM_MODEL in .env to a model the provider currently serves "
                "(Groq lists its models at https://console.groq.com/docs/models)."
            )
        if isinstance(exc, openai.APIStatusError):
            if hint := self._rate_limit_hint(exc):
                return LLMError(hint)
            return LLMError(f"{self.provider} API error {exc.status_code}: {exc}")
        if isinstance(exc, openai.APIConnectionError):
            return LLMError(f"Could not reach {self.provider} at {self._client.base_url}: {exc}")
        return LLMError(f"{self.provider} request failed: {exc}")

    # ---- main turn ---------------------------------------------------------

    def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        on_text: Callable[[str], None] | None = None,
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.settings.agent_max_tokens,
            "messages": [{"role": "system", "content": system}, *messages],
        }
        if tools:
            payload["tools"] = self._to_openai_tools(tools)
            payload["tool_choice"] = "auto"
            # Small open models fan out to many identical calls if allowed;
            # keeping turns single-call makes the loop far more reliable.
            if self.settings.llm_disable_parallel_tool_calls:
                payload["parallel_tool_calls"] = False

        completion = self._create_with_tool_parse_retry(payload)

        if not completion.choices:
            raise LLMError(f"{self.provider} returned no choices.")

        choice = completion.choices[0]
        message = choice.message
        stop_reason = _STOP_REASONS.get(choice.finish_reason or "stop", "end_turn")

        if stop_reason == "refusal":
            raise RefusalError(
                f"{self.provider} blocked this request (finish_reason=content_filter). "
                "Rephrase the instruction and try again."
            )

        text = (message.content or "").strip()
        tool_calls = self._parse_tool_calls(message)
        if tool_calls:
            # Some providers report finish_reason="stop" even when they emitted
            # tool calls; trust the presence of the calls.
            stop_reason = "tool_use"

        if on_text and text:
            on_text(text)

        usage = self._accumulate_usage(completion.usage)
        log.debug(
            "llm.turn",
            provider=self.provider,
            stop_reason=stop_reason,
            tool_calls=[call.name for call in tool_calls],
            **usage,
        )

        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            raw_content=message,
            usage=usage,
            model=completion.model or self.model,
        )

    def _parse_tool_calls(self, message: Any) -> list[ToolCall]:
        calls: list[ToolCall] = []
        for index, raw in enumerate(getattr(message, "tool_calls", None) or []):
            function = getattr(raw, "function", None)
            if function is None:
                continue
            raw_arguments = function.arguments or "{}"
            try:
                arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
            except json.JSONDecodeError:
                # A weaker model produced malformed JSON. Don't crash the run —
                # hand it back empty so the tool's own validation reports the
                # problem to the model, which can then retry properly.
                log.warning(
                    "llm.tool_arguments.invalid_json",
                    tool=function.name,
                    raw=str(raw_arguments)[:200],
                )
                arguments = {}
            if not isinstance(arguments, dict):
                arguments = {}
            calls.append(
                ToolCall(id=getattr(raw, "id", None) or f"call_{index}", name=function.name, arguments=arguments)
            )
        return calls

    # ---- structured output (planner) ---------------------------------------

    def structured(
        self,
        *,
        system: str,
        prompt: str,
        schema: dict[str, Any],
        max_tokens: int = 4096,
    ) -> dict[str, Any]:
        instruction = (
            f"{prompt}\n\n"
            "Reply with a single JSON object and nothing else — no prose, no markdown "
            f"fences. It must validate against this JSON Schema:\n{json.dumps(schema, indent=2)}"
        )
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": instruction},
            ],
            "response_format": {"type": "json_object"},
        }

        try:
            completion = self._client.chat.completions.create(**payload)
        except Exception as exc:  # noqa: BLE001
            # Not every model supports response_format; retry as plain text.
            log.warning("llm.structured.retry_without_response_format", error=str(exc))
            payload.pop("response_format")
            try:
                completion = self._client.chat.completions.create(**payload)
            except Exception as inner:  # noqa: BLE001
                raise self._wrap_api_errors(inner) from inner

        self._accumulate_usage(completion.usage)
        text = (completion.choices[0].message.content or "").strip()
        return _parse_json_object(text)

    # ---- message construction ---------------------------------------------

    def build_assistant_message(self, response: LLMResponse) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": response.text or None}
        if response.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
                }
                for call in response.tool_calls
            ]
        return message

    def build_tool_result_messages(self, outcomes: list[ToolOutcome]) -> list[dict[str, Any]]:
        # One `role: "tool"` message per call — the API rejects a batched one.
        return [
            {"role": "tool", "tool_call_id": outcome.call_id, "content": outcome.content}
            for outcome in outcomes
        ]


def _parse_json_object(text: str) -> dict[str, Any]:
    """Best-effort JSON extraction from a model reply."""
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Last resort: the outermost {...} span.
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass

    raise LLMError(f"Model returned invalid JSON: {text[:300]}")
