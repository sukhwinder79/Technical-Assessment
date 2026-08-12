"""Provider-agnostic LLM interface.

The executor never imports the Anthropic SDK. It talks to this interface, so
swapping in another provider means writing one adapter class — the agent loop,
the tool registry and the planner are untouched.

Message *shape* stays provider-native — Anthropic content blocks carry thinking
signatures that must be replayed verbatim, while OpenAI-compatible APIs want
`tool_calls` on the assistant turn and a separate `role: "tool"` message per
result. Building those turns is therefore delegated back to the adapter via
`build_assistant_message` / `build_tool_result_messages`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(slots=True)
class ToolOutcome:
    call_id: str
    content: str
    is_error: bool = False


@dataclass(slots=True)
class LLMResponse:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = "end_turn"
    raw_content: Any = None
    usage: dict[str, int] = field(default_factory=dict)
    model: str = ""

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


@runtime_checkable
class LLMClient(Protocol):
    """The contract the agent depends on."""

    model: str

    def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        on_text: Callable[[str], None] | None = None,
    ) -> LLMResponse:
        """One assistant turn. `on_text` receives streamed text deltas."""
        ...

    def structured(
        self,
        *,
        system: str,
        prompt: str,
        schema: dict[str, Any],
        max_tokens: int = 4096,
    ) -> dict[str, Any]:
        """One turn constrained to a JSON Schema. Returns the parsed object."""
        ...

    def build_assistant_message(self, response: LLMResponse) -> dict[str, Any]:
        """Render an assistant turn for replay in the next request."""
        ...

    def build_tool_result_messages(self, outcomes: list[ToolOutcome]) -> list[dict[str, Any]]:
        """Render tool results as history messages.

        Returns a *list* because providers disagree: Anthropic requires every
        result for one turn batched into a single user message, while
        OpenAI-compatible APIs require one `role: "tool"` message per call.
        """
        ...
