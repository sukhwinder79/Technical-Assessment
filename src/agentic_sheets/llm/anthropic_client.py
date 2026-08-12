"""Anthropic (Claude) implementation of `LLMClient`.

Uses native tool calling with adaptive thinking. Streaming is the default so
long tool-planning turns cannot hit an HTTP timeout, and so the CLI can show
the model's narration live while it works.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from ..config import Settings
from ..errors import ConfigurationError, LLMError, RefusalError
from ..logging_setup import get_logger
from .base import LLMResponse, ToolCall, ToolOutcome

log = get_logger(__name__)


class AnthropicLLMClient:
    """Thin, explicit wrapper around `anthropic.Anthropic`."""

    def __init__(self, settings: Settings) -> None:
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise ConfigurationError(
                "The 'anthropic' package is not installed. Run: pip install -r requirements.txt"
            ) from exc

        api_key = settings.resolved_llm_api_key()
        if not api_key:
            raise ConfigurationError(
                "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and add your key — "
                "or set LLM_PROVIDER=groq with a free GROQ_API_KEY instead."
            )

        self._anthropic = anthropic
        self._client = anthropic.Anthropic(api_key=api_key, max_retries=3)
        self.settings = settings
        self.provider = "anthropic"
        self.model = settings.resolved_llm_model()
        self.total_usage: dict[str, int] = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
        }

    # ---- request construction ---------------------------------------------

    def _base_kwargs(self, system: str) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.settings.agent_max_tokens,
            # A large, stable system prompt is worth caching across turns.
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            # Adaptive thinking: Claude decides how much to reason per turn.
            "thinking": {"type": "adaptive"},
        }
        if self.settings.agent_effort:
            kwargs["output_config"] = {"effort": self.settings.agent_effort}
        return kwargs

    def _accumulate_usage(self, usage: Any) -> dict[str, int]:
        snapshot = {
            "input_tokens": getattr(usage, "input_tokens", 0) or 0,
            "output_tokens": getattr(usage, "output_tokens", 0) or 0,
            "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
        }
        for key, value in snapshot.items():
            self.total_usage[key] += value
        return snapshot

    # ---- main turn ---------------------------------------------------------

    def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        on_text: Callable[[str], None] | None = None,
    ) -> LLMResponse:
        kwargs = self._base_kwargs(system)
        kwargs["messages"] = messages
        if tools:
            kwargs["tools"] = tools

        try:
            with self._client.messages.stream(**kwargs) as stream:
                for event in stream:
                    if (
                        on_text is not None
                        and event.type == "content_block_delta"
                        and getattr(event.delta, "type", None) == "text_delta"
                    ):
                        on_text(event.delta.text)
                message = stream.get_final_message()
        except self._anthropic.AuthenticationError as exc:
            raise ConfigurationError(f"Anthropic rejected the API key: {exc}") from exc
        except self._anthropic.NotFoundError as exc:
            raise ConfigurationError(
                f"Model '{self.model}' was not found. Check AGENT_MODEL in .env. ({exc})"
            ) from exc
        except self._anthropic.APIStatusError as exc:
            raise LLMError(f"Anthropic API error {exc.status_code}: {exc.message}") from exc
        except self._anthropic.APIConnectionError as exc:
            raise LLMError(f"Could not reach the Anthropic API: {exc}") from exc

        if message.stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            category = getattr(details, "category", None)
            raise RefusalError(
                "The model declined this request"
                + (f" (category: {category})" if category else "")
                + ". Rephrase the instruction and try again."
            )

        usage = self._accumulate_usage(message.usage)

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in message.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                arguments = block.input if isinstance(block.input, dict) else {}
                tool_calls.append(ToolCall(id=block.id, name=block.name, arguments=arguments))

        log.debug(
            "llm.turn",
            stop_reason=message.stop_reason,
            tool_calls=[c.name for c in tool_calls],
            **usage,
        )

        return LLMResponse(
            text="".join(text_parts).strip(),
            tool_calls=tool_calls,
            stop_reason=message.stop_reason or "end_turn",
            raw_content=message.content,
            usage=usage,
            model=message.model,
        )

    # ---- structured output (used by the planner) ---------------------------

    def structured(
        self,
        *,
        system: str,
        prompt: str,
        schema: dict[str, Any],
        max_tokens: int = 4096,
    ) -> dict[str, Any]:
        kwargs = self._base_kwargs(system)
        kwargs["max_tokens"] = max_tokens
        kwargs["messages"] = [{"role": "user", "content": prompt}]
        kwargs["output_config"] = {
            **kwargs.get("output_config", {}),
            "format": {"type": "json_schema", "schema": schema},
        }

        try:
            message = self._client.messages.create(**kwargs)
        except Exception as exc:  # noqa: BLE001
            # Structured outputs are not available on every model. Rather than
            # failing the run, fall back to plain JSON prompting.
            log.warning("llm.structured.unavailable", error=str(exc))
            return self._structured_fallback(system=system, prompt=prompt, schema=schema, max_tokens=max_tokens)

        self._accumulate_usage(message.usage)
        text = next((b.text for b in message.content if b.type == "text"), "")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError(f"Model returned invalid JSON for a structured request: {exc}") from exc

    def _structured_fallback(
        self, *, system: str, prompt: str, schema: dict[str, Any], max_tokens: int
    ) -> dict[str, Any]:
        instruction = (
            f"{prompt}\n\n"
            "Respond with a single JSON object and nothing else — no prose, no markdown fences. "
            f"It must validate against this JSON Schema:\n{json.dumps(schema, indent=2)}"
        )
        kwargs = self._base_kwargs(system)
        kwargs["max_tokens"] = max_tokens
        kwargs["messages"] = [{"role": "user", "content": instruction}]
        message = self._client.messages.create(**kwargs)
        self._accumulate_usage(message.usage)
        text = next((b.text for b in message.content if b.type == "text"), "").strip()
        if text.startswith("```"):
            text = text.split("```", 2)[1]
            text = text[4:] if text.lower().startswith("json") else text
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError(f"Model returned invalid JSON: {text[:300]}") from exc

    # ---- message construction ---------------------------------------------

    def build_assistant_message(self, response: LLMResponse) -> dict[str, Any]:
        # Pass the native content back verbatim — thinking blocks carry
        # signatures the API validates, so they must not be rebuilt by hand.
        return {"role": "assistant", "content": response.raw_content}

    def build_tool_result_messages(self, outcomes: list[ToolOutcome]) -> list[dict[str, Any]]:
        # All results for one assistant turn go back in a SINGLE user message,
        # otherwise Claude learns to stop issuing parallel tool calls.
        blocks = [
            {
                "type": "tool_result",
                "tool_use_id": outcome.call_id,
                "content": outcome.content,
                **({"is_error": True} if outcome.is_error else {}),
            }
            for outcome in outcomes
        ]
        return [{"role": "user", "content": blocks}]
