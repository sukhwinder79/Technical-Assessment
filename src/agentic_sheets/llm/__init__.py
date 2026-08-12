"""LLM provider abstraction.

The agent depends only on the `LLMClient` protocol, so adding a provider means
writing one adapter and adding a line to the factory below — the tool registry,
planner and executor are untouched.

Two adapters ship:

* `AnthropicLLMClient`        — Claude, with adaptive thinking and effort control.
* `OpenAICompatibleLLMClient` — Groq (free tier), OpenAI, OpenRouter, Together
  and local Ollama, which all speak the same chat-completions wire format.
"""

from __future__ import annotations

from ..config import Settings
from .base import LLMClient, LLMResponse, ToolCall, ToolOutcome


def build_client(settings: Settings) -> LLMClient:
    """Construct the client for the resolved provider."""
    if settings.uses_anthropic():
        from .anthropic_client import AnthropicLLMClient

        return AnthropicLLMClient(settings)

    from .openai_compatible_client import OpenAICompatibleLLMClient

    return OpenAICompatibleLLMClient(settings)


__all__ = ["LLMClient", "LLMResponse", "ToolCall", "ToolOutcome", "build_client"]
