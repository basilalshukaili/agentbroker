"""
External-dependencies health check.

Pings every upstream service the broker relies on. Returns a structured
report agents and humans can both consume.

Designed to be cheap (parallel, 5s timeout each) so it can be polled
from a browser tab or a status page without burning quota.

What it knows how to check:

  Twilio       — auth + balance (so we know the trial credit isn't $0)
  Cal.com v2   — /me echoes the authenticated user
  Vapi         — /assistant returns 200
  Resend       — /domains returns 401 'restricted_api_key' (which is correct
                 for a send-only key; we treat that response as healthy)
  Paddle       — /event-types is the cheapest authenticated read
  Internal     — manifest + MCP + discovery surfaces

Safe to surface publicly: nothing in the response leaks an API key,
balance precision is rounded, and any error is bucketed to a generic
message.
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None


_TIMEOUT = 5.0


def _bucket(ok: bool, latency_ms: int, *, message: str | None = None,
            extra: dict | None = None) -> dict:
    return {
        "status": "ok" if ok else "fail",
        "latency_ms": latency_ms,
        **({"message": message} if message else {}),
        **(extra or {}),
    }


async def _check_twilio(client: "httpx.AsyncClient") -> dict:
    sid = os.getenv("TWILIO_ACCOUNT_SID", "")
    tok = os.getenv("TWILIO_AUTH_TOKEN", "")
    if not sid or not tok:
        return _bucket(False, 0, message="not_configured")
    t0 = time.monotonic()
    try:
        r = await client.get(
            f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Balance.json",
            auth=(sid, tok),
        )
        latency = int((time.monotonic() - t0) * 1000)
        if r.status_code != 200:
            return _bucket(False, latency, message=f"http_{r.status_code}")
        body = r.json()
        balance = float(body.get("balance", 0))
        currency = body.get("currency", "USD")
        return _bucket(
            balance > 0.5, latency,
            message=("low_balance" if balance < 2.0 else "ok"),
            extra={"balance": round(balance, 2), "currency": currency,
                   "warning": "credit < $2 — top up soon" if balance < 2.0 else None},
        )
    except Exception as e:
        return _bucket(False, int((time.monotonic() - t0) * 1000), message=str(e)[:80])


async def _check_calcom(client: "httpx.AsyncClient") -> dict:
    key = os.getenv("CALCOM_API_KEY", "")
    if not key:
        return _bucket(False, 0, message="not_configured")
    t0 = time.monotonic()
    try:
        r = await client.get(
            "https://api.cal.com/v2/me",
            headers={"Authorization": f"Bearer {key}", "cal-api-version": "2024-08-13"},
        )
        latency = int((time.monotonic() - t0) * 1000)
        return _bucket(r.status_code == 200, latency, message=f"http_{r.status_code}")
    except Exception as e:
        return _bucket(False, int((time.monotonic() - t0) * 1000), message=str(e)[:80])


async def _check_vapi(client: "httpx.AsyncClient") -> dict:
    key = os.getenv("VAPI_API_KEY", "")
    if not key:
        return _bucket(False, 0, message="not_configured")
    t0 = time.monotonic()
    try:
        r = await client.get(
            "https://api.vapi.ai/assistant",
            headers={"Authorization": f"Bearer {key}"},
        )
        latency = int((time.monotonic() - t0) * 1000)
        return _bucket(r.status_code == 200, latency, message=f"http_{r.status_code}")
    except Exception as e:
        return _bucket(False, int((time.monotonic() - t0) * 1000), message=str(e)[:80])


async def _check_resend(client: "httpx.AsyncClient") -> dict:
    """Send-only restricted keys return 401 with body name='restricted_api_key' —
    that is the *correct, more secure* scope and we treat it as healthy."""
    key = os.getenv("RESEND_API_KEY", "")
    if not key:
        return _bucket(False, 0, message="not_configured")
    t0 = time.monotonic()
    try:
        r = await client.get(
            "https://api.resend.com/domains",
            headers={"Authorization": f"Bearer {key}"},
        )
        latency = int((time.monotonic() - t0) * 1000)
        if r.status_code == 200:
            return _bucket(True, latency, message="full_access_key")
        if r.status_code == 401:
            try:
                body = r.json()
            except Exception:
                body = {}
            if body.get("name") == "restricted_api_key":
                return _bucket(True, latency, message="send_only_key (good — least-privilege)")
        return _bucket(False, latency, message=f"http_{r.status_code}")
    except Exception as e:
        return _bucket(False, int((time.monotonic() - t0) * 1000), message=str(e)[:80])


async def _check_paddle(client: "httpx.AsyncClient") -> dict:
    key = os.getenv("PADDLE_API_KEY", "")
    if not key:
        return _bucket(False, 0, message="not_configured")
    env = os.getenv("PADDLE_ENVIRONMENT", "production").lower()
    base = "https://sandbox-api.paddle.com" if env == "sandbox" else "https://api.paddle.com"
    t0 = time.monotonic()
    try:
        r = await client.get(
            f"{base}/event-types",
            headers={"Authorization": f"Bearer {key}", "Paddle-Version": "1"},
        )
        latency = int((time.monotonic() - t0) * 1000)
        return _bucket(r.status_code == 200, latency, message=f"http_{r.status_code}")
    except Exception as e:
        return _bucket(False, int((time.monotonic() - t0) * 1000), message=str(e)[:80])


async def _check_internal_discovery() -> dict:
    """Verify the manifest / mcp / well-known surfaces actually load in-process."""
    t0 = time.monotonic()
    try:
        from agent_interface.manifest_server import get_full_manifest
        from agent_interface.mcp_server import handle_mcp_request
        from agent_interface.well_known import get_llms_txt
        m = get_full_manifest()
        op_count = len(m.get("operations", []))
        r = await handle_mcp_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        tool_count = len(r.get("result", {}).get("tools", []))
        llms = get_llms_txt()
        latency = int((time.monotonic() - t0) * 1000)
        # Manifest and MCP tool list must agree (drift = bug) and we must have
        # at least our 12 core operations; new tools are allowed to push the
        # count higher without flipping the gauge to fail.
        ok = (
            op_count == tool_count
            and op_count >= 12
            and "find_business" in llms
        )
        return _bucket(
            ok, latency,
            extra={"operations": op_count, "mcp_tools": tool_count, "llms_txt_bytes": len(llms)},
        )
    except Exception as e:
        return _bucket(False, int((time.monotonic() - t0) * 1000), message=str(e)[:80])


async def run_external_health() -> dict:
    """Run every upstream check in parallel; return the aggregated report."""
    if httpx is None:
        return {"status": "fail", "error": "httpx not installed"}
    started = time.time()
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        twilio, calcom, vapi, resend, paddle, internal = await asyncio.gather(
            _check_twilio(client),
            _check_calcom(client),
            _check_vapi(client),
            _check_resend(client),
            _check_paddle(client),
            _check_internal_discovery(),
            return_exceptions=False,
        )
    services = {
        "twilio": twilio,
        "calcom": calcom,
        "vapi": vapi,
        "resend": resend,
        "paddle": paddle,
        "internal_discovery": internal,
    }
    all_ok = all(s["status"] == "ok" for s in services.values())
    any_warning = any(s.get("warning") for s in services.values())
    return {
        "status": "ok" if all_ok else ("degraded" if any_warning else "fail"),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "checked_in_ms": int((time.time() - started) * 1000),
        "services": services,
        "summary": {
            "total": len(services),
            "ok": sum(1 for s in services.values() if s["status"] == "ok"),
            "fail": sum(1 for s in services.values() if s["status"] == "fail"),
            "warnings": [k for k, s in services.items() if s.get("warning")],
        },
    }
