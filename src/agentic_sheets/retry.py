"""Retry with exponential backoff + jitter.

Deliberately small and dependency-free so it can be unit-tested with a fake
clock. Only `ToolError(retryable=True)` and unexpected exceptions are retried —
a validation error or a missing credentials file fails fast and goes back to
the model, which is far more useful than three identical failures.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Callable, TypeVar

from .errors import ToolError

T = TypeVar("T")


@dataclass(slots=True)
class RetryPolicy:
    max_retries: int = 2
    base_delay: float = 1.0
    max_delay: float = 20.0
    jitter: float = 0.25

    def delay_for(self, attempt: int) -> float:
        """Delay before attempt N (1-indexed retry number)."""
        raw = min(self.base_delay * (2 ** (attempt - 1)), self.max_delay)
        return raw + random.uniform(0, self.jitter * raw)


def is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, ToolError):
        return exc.retryable
    # Anything unexpected gets one more shot; a genuine bug will fail twice
    # and be reported, but a flaky COM/HTTP call usually recovers.
    return True


def call_with_retry(
    fn: Callable[[], T],
    policy: RetryPolicy,
    *,
    on_retry: Callable[[int, BaseException, float], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    attempt = 0
    while True:
        try:
            return fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            if attempt >= policy.max_retries or not is_retryable(exc):
                raise
            attempt += 1
            delay = policy.delay_for(attempt)
            if on_retry:
                on_retry(attempt, exc, delay)
            sleep(delay)
