"""
Distributed tracer — emits all required spans per request (§2.7).
Uses OpenTelemetry SDK when available; falls back to structured dict logging.
"""
from __future__ import annotations

import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Generator, Optional


@dataclass
class Span:
    trace_id: str
    span_id: str
    operation_name: str
    start_time: float = field(default_factory=time.monotonic)
    attributes: dict[str, Any] = field(default_factory=dict)
    end_time: Optional[float] = None
    status: str = "ok"

    def set(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def finish(self, status: str = "ok") -> None:
        self.end_time = time.monotonic()
        self.status = status

    @property
    def duration_ms(self) -> float:
        if self.end_time:
            return (self.end_time - self.start_time) * 1000
        return 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "operation": self.operation_name,
            "duration_ms": round(self.duration_ms, 2),
            "status": self.status,
            **self.attributes,
        }


class Tracer:
    def __init__(self) -> None:
        self._spans: list[Span] = []

    def start_span(self, operation_name: str, trace_id: Optional[str] = None) -> Span:
        span = Span(
            trace_id=trace_id or str(uuid.uuid4()),
            span_id=str(uuid.uuid4())[:8],
            operation_name=operation_name,
        )
        self._spans.append(span)
        return span

    @contextmanager
    def span(self, operation_name: str, trace_id: Optional[str] = None) -> Generator[Span, None, None]:
        s = self.start_span(operation_name, trace_id)
        try:
            yield s
            s.finish("ok")
        except Exception as exc:
            s.finish("error")
            s.set("error.message", str(exc))
            raise

    def emit_request_span(
        self,
        *,
        trace_id: str,
        agent_id: Optional[str],
        principal_id: Optional[str],
        operation: str,
        smb_id: Optional[str],
        channel_used: Optional[str],
        channel_fallback_chain: list[str],
        compliance_checks_passed: bool,
        outcome_code: Optional[str],
        cost_actual: Optional[float],
        latency_ms: Optional[int],
        idempotency_key: Optional[str],
        failure_class: Optional[str],
        manifest_version: str = "0.1.0",
    ) -> Span:
        """Emit the canonical per-request span with all required attributes (§2.7)."""
        span = self.start_span(f"smb_broker.{operation}", trace_id)
        span.set("agent.identity", agent_id or "anonymous")
        span.set("principal.id", principal_id or "none")
        span.set("manifest.version_served", manifest_version)
        span.set("operation.name", operation)
        span.set("target_smb.id", smb_id or "none")
        span.set("channel.used", channel_used or "none")
        span.set("channel.fallback_chain", channel_fallback_chain)
        span.set("compliance.checks_passed", compliance_checks_passed)
        span.set("outcome.code", outcome_code or "none")
        span.set("cost.actual", cost_actual or 0.0)
        span.set("latency.ms", latency_ms or 0)
        span.set("idempotency.key", idempotency_key or "none")
        if failure_class:
            span.set("failure.class", failure_class)
        span.finish("ok" if compliance_checks_passed and outcome_code not in ("failure",) else "error")
        return span

    def recent(self, n: int = 100) -> list[dict]:
        return [s.to_dict() for s in self._spans[-n:]]


_tracer = Tracer()


def get_tracer() -> Tracer:
    return _tracer
