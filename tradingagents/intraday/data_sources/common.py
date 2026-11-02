"""Shared bounded retry and rate-limit helpers for read-only data clients."""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import TypeVar

T = TypeVar("T")


class ProviderError(RuntimeError):
    """Safe provider error that never embeds credentials or response headers."""


@dataclass
class RateLimiter:
    requests_per_second: float = 10.0
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    _next_at: float = 0.0

    def wait(self) -> None:
        if self.requests_per_second <= 0:
            raise ValueError("requests_per_second must be positive")
        now = self.clock()
        delay = max(0.0, self._next_at - now)
        if delay:
            self.sleep(delay)
        self._next_at = max(now, self._next_at) + 1.0 / self.requests_per_second


def retry_call(
    operation: Callable[[], T],
    *,
    attempts: int = 5,
    sleep: Callable[[float], None] = time.sleep,
    retryable: Callable[[Exception], bool],
    retry_after: Callable[[Exception], str | None] = lambda _exc: None,
    random_value: Callable[[], float] = random.random,
) -> T:
    """Retry bounded transient failures; never retries non-retryable 4xx errors."""
    if attempts < 1:
        raise ValueError("attempts must be at least one")
    for attempt in range(attempts):
        try:
            return operation()
        except Exception as exc:
            if attempt + 1 == attempts or not retryable(exc):
                raise
            delay = None
            header = retry_after(exc)
            if header:
                try:
                    delay = max(0.0, float(header))
                except ValueError:
                    try:
                        delay = max(0.0, (parsedate_to_datetime(header).timestamp() - time.time()))
                    except (TypeError, ValueError, OverflowError):
                        delay = None
            sleep(delay if delay is not None else min(30.0, (2**attempt) + random_value()))
    raise AssertionError("unreachable")
