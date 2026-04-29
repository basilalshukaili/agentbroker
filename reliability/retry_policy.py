"""
Retry policy — exponential backoff with jitter for retriable errors.
"""
from __future__ import annotations

import asyncio
import random
from typing import Callable, TypeVar, Awaitable

T = TypeVar("T")

_RETRIABLE_CODES = {
    "upstream_failure", "transient", "rate_limited", "supply_unreachable"
}


def is_retriable(reason_code: str) -> bool:
    return reason_code in _RETRIABLE_CODES


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    max_attempts: int = 3,
    base_delay_s: float = 1.0,
    max_delay_s: float = 30.0,
    should_retry: Callable[[Exception], bool] = lambda _: True,
) -> T:
    """
    Execute fn with exponential backoff retry.
    Raises last exception if all attempts fail.
    """
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            return await fn()
        except Exception as exc:
            last_exc = exc
            if not should_retry(exc) or attempt == max_attempts - 1:
                raise
            delay = min(base_delay_s * (2 ** attempt) + random.uniform(0, 1), max_delay_s)
            await asyncio.sleep(delay)
    raise last_exc  # type: ignore
