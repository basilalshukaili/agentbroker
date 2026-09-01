"""
/healthz/external must not probe a rail we never wired.

THE INCIDENT, 2026-09-01. The gauge reported overall status "fail" continuously
while every real dependency was green. The single failing service was "paddle":
the probe read PADDLE_API_KEY, which does not exist on the production service,
and the request came back HTTP 403 on every poll. Paddle was evaluated and never
adopted - the live rails are credits, x402 and Polar - so the check could never
have gone green no matter what we fixed upstream.

A probe that can only ever fail does not report a problem; it teaches whoever
reads the gauge to ignore a red light. That is strictly worse than not having
the gauge, because the next genuine outage looks identical to the noise.

THE RULE THESE TESTS ENFORCE: the report carries a key per service we actually
depend on, and "paddle" is not one of them. The second test pins the rest of
the response shape so that removing the key was the ONLY change - the summary
counters and the top-level fields are what callers already parse.
"""
from __future__ import annotations

import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent_interface import health_external  # noqa: E402
from agent_interface.health_external import run_external_health  # noqa: E402


# Every upstream credential is cleared so the probes short-circuit on
# "not_configured" and the suite makes no network call (bill-safety: these
# checks hit paid vendors).
_UPSTREAM_ENV = (
    "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN",
    "CALCOM_API_KEY", "VAPI_API_KEY", "RESEND_API_KEY",
)


def _report(monkeypatch) -> dict:
    for name in _UPSTREAM_ENV:
        monkeypatch.delenv(name, raising=False)
    return asyncio.run(run_external_health())


def test_paddle_is_not_a_checked_service(monkeypatch):
    report = _report(monkeypatch)
    assert "paddle" not in report["services"], (
        "the Paddle probe is deliberately removed - it has no API key in any "
        "environment, returns HTTP 403 forever, and was the only reason this "
        "gauge read 'fail'")
    assert not hasattr(health_external, "_check_paddle"), (
        "_check_paddle was re-added; see the comment where it used to live")


def test_report_shape_is_otherwise_unchanged(monkeypatch):
    report = _report(monkeypatch)

    assert set(report) == {
        "status", "timestamp", "checked_in_ms", "services", "summary",
    }
    assert set(report["services"]) == {
        "twilio", "calcom", "vapi", "resend", "internal_discovery",
    }
    assert set(report["summary"]) == {"total", "ok", "fail", "warnings"}
    assert report["summary"]["total"] == len(report["services"]) == 5
    assert report["summary"]["ok"] + report["summary"]["fail"] == 5

    for name, svc in report["services"].items():
        assert svc["status"] in ("ok", "fail"), name
        assert isinstance(svc["latency_ms"], int), name


def test_internal_discovery_alone_can_carry_the_gauge_green(monkeypatch):
    """With no upstream keys the gauge must still be honest: the in-process
    discovery surfaces are checked for real and report their own verdict."""
    report = _report(monkeypatch)
    internal = report["services"]["internal_discovery"]
    assert internal["status"] == "ok", internal
    assert internal["operations"] == internal["mcp_tools"]
    # Unconfigured upstreams are a genuine fail, not a fabricated pass.
    assert report["services"]["twilio"]["message"] == "not_configured"
    assert report["status"] == "fail"
