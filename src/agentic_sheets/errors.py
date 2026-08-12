"""Error types shared across the agent.

The key distinction is `retryable`: the executor's retry policy only re-runs a
tool when the failure is plausibly transient (COM busy, network blip, 5xx).
A bad argument or a missing credentials file is returned to the model straight
away so it can *reason* about the failure instead of hammering the same call.
"""

from __future__ import annotations


class AgentError(Exception):
    """Base class for every error this package raises deliberately."""


class ConfigurationError(AgentError):
    """Something in .env / tools.yaml / credentials is missing or wrong."""


class ToolError(AgentError):
    """A tool failed. Surfaced back to the model as an `is_error` tool_result.

    Args:
        message: Human/model readable explanation.
        retryable: Whether the executor should retry with backoff.
        remediation: Concrete next step the model (or user) can take.
        details: Extra structured context for logs and the run report.
    """

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        remediation: str | None = None,
        details: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.retryable = retryable
        self.remediation = remediation
        self.details = details or {}

    def to_payload(self) -> dict:
        payload: dict = {"ok": False, "error": self.message}
        if self.remediation:
            payload["remediation"] = self.remediation
        if self.details:
            payload["details"] = self.details
        return payload


class ToolNotFoundError(AgentError):
    """The model asked for a tool that is not registered or is disabled."""


class LLMError(AgentError):
    """The model provider failed in a way the agent cannot recover from."""


class RefusalError(LLMError):
    """The model declined the request (stop_reason == 'refusal')."""
