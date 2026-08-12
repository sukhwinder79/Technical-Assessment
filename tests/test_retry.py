"""Retry policy: what gets retried, what fails fast, and the backoff shape."""

from __future__ import annotations

import pytest

from agentic_sheets.errors import ToolError
from agentic_sheets.retry import RetryPolicy, call_with_retry, is_retryable


def test_returns_immediately_on_success():
    calls = []
    result = call_with_retry(lambda: calls.append(1) or "ok", RetryPolicy(max_retries=3), sleep=lambda _: None)
    assert result == "ok"
    assert len(calls) == 1


def test_retries_a_retryable_tool_error_then_succeeds():
    attempts = {"n": 0}
    slept: list[float] = []

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ToolError("Excel is busy", retryable=True)
        return "recovered"

    result = call_with_retry(
        flaky, RetryPolicy(max_retries=3, base_delay=0.5), sleep=slept.append
    )
    assert result == "recovered"
    assert attempts["n"] == 3
    assert len(slept) == 2
    assert slept[1] > slept[0]  # exponential


def test_non_retryable_tool_error_fails_on_the_first_attempt():
    attempts = {"n": 0}

    def bad_input():
        attempts["n"] += 1
        raise ToolError("CSV not found", retryable=False)

    with pytest.raises(ToolError):
        call_with_retry(bad_input, RetryPolicy(max_retries=5), sleep=lambda _: None)
    assert attempts["n"] == 1


def test_gives_up_after_max_retries_and_reraises():
    attempts = {"n": 0}

    def always_fails():
        attempts["n"] += 1
        raise ToolError("rate limited", retryable=True)

    with pytest.raises(ToolError):
        call_with_retry(always_fails, RetryPolicy(max_retries=2), sleep=lambda _: None)
    assert attempts["n"] == 3  # initial + 2 retries


def test_on_retry_callback_reports_progress():
    seen: list[tuple[int, str]] = []
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise ToolError("blip", retryable=True)
        return "ok"

    call_with_retry(
        flaky,
        RetryPolicy(max_retries=2),
        on_retry=lambda attempt, exc, delay: seen.append((attempt, str(exc))),
        sleep=lambda _: None,
    )
    assert seen == [(1, "blip")]


def test_zero_retries_disables_the_policy():
    attempts = {"n": 0}

    def always_fails():
        attempts["n"] += 1
        raise ToolError("nope", retryable=True)

    with pytest.raises(ToolError):
        call_with_retry(always_fails, RetryPolicy(max_retries=0), sleep=lambda _: None)
    assert attempts["n"] == 1


def test_delay_is_capped():
    policy = RetryPolicy(base_delay=10, max_delay=15, jitter=0)
    assert policy.delay_for(1) == 10
    assert policy.delay_for(5) == 15


def test_unexpected_exceptions_are_retryable_but_tool_errors_respect_their_flag():
    assert is_retryable(RuntimeError("who knows"))
    assert is_retryable(ToolError("busy", retryable=True))
    assert not is_retryable(ToolError("bad path", retryable=False))
