"""
Circuit breaker — prevents cascade failures to external channels.
States: CLOSED (normal) → OPEN (failing) → HALF_OPEN (probing recovery).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreaker:
    name: str
    failure_threshold: int = 5        # failures before opening
    recovery_timeout_s: float = 60.0  # seconds before trying half-open
    success_threshold: int = 2        # successes to close from half-open

    _state: CircuitState = field(default=CircuitState.CLOSED, init=False)
    _failure_count: int = field(default=0, init=False)
    _success_count: int = field(default=0, init=False)
    _opened_at: float = field(default=0.0, init=False)

    def is_available(self) -> bool:
        if self._state == CircuitState.OPEN:
            if time.monotonic() - self._opened_at >= self.recovery_timeout_s:
                self._state = CircuitState.HALF_OPEN
                return True
            return False
        return True

    def record_success(self) -> None:
        if self._state == CircuitState.HALF_OPEN:
            self._success_count += 1
            if self._success_count >= self.success_threshold:
                self._state = CircuitState.CLOSED
                self._failure_count = 0
                self._success_count = 0
        elif self._state == CircuitState.CLOSED:
            self._failure_count = max(0, self._failure_count - 1)

    def record_failure(self) -> None:
        self._failure_count += 1
        self._success_count = 0
        if self._failure_count >= self.failure_threshold:
            self._state = CircuitState.OPEN
            self._opened_at = time.monotonic()

    @property
    def state(self) -> CircuitState:
        return self._state


# Registry of breakers keyed by channel name
_breakers: dict[str, CircuitBreaker] = {}


def get_breaker(channel_name: str) -> CircuitBreaker:
    if channel_name not in _breakers:
        _breakers[channel_name] = CircuitBreaker(name=channel_name)
    return _breakers[channel_name]


def all_breakers() -> dict[str, CircuitBreaker]:
    return dict(_breakers)
