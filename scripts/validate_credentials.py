"""
Credential validation script.

Run this after editing .env to verify every external service is reachable
with the supplied credentials. Reports per-service status; does not send
real messages or charge money.

Usage:
    python scripts/validate_credentials.py
    python scripts/validate_credentials.py --verbose
"""
from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass

# Make the project root importable regardless of where this is invoked from.
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


@dataclass
class CheckResult:
    name: str
    ok: bool
    message: str
    latency_ms: float = 0.0


# ---------------------------------------------------------------------------
# Loader: read .env if it exists
# ---------------------------------------------------------------------------

def _load_dotenv():
    path = ".env"
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            v = v.strip().strip('"').strip("'")
            if k and v and k.strip() not in os.environ:
                os.environ[k.strip()] = v


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

async def check_twilio() -> CheckResult:
    import time
    sid = os.getenv("TWILIO_ACCOUNT_SID", "")
    token = os.getenv("TWILIO_AUTH_TOKEN", "")
    if not sid or not token:
        return CheckResult("Twilio", False, "TWILIO_ACCOUNT_SID or TWILIO_AUTH_TOKEN missing.")
    try:
        import httpx
        start = time.time()
        async with httpx.AsyncClient(auth=(sid, token), timeout=10.0) as client:
            r = await client.get(f"https://api.twilio.com/2010-04-01/Accounts/{sid}.json")
        elapsed = (time.time() - start) * 1000
        if r.status_code == 200:
            data = r.json()
            return CheckResult(
                "Twilio", True,
                f"Account '{data.get('friendly_name', sid[:6])}' status={data.get('status')}",
                elapsed,
            )
        return CheckResult("Twilio", False, f"HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        return CheckResult("Twilio", False, f"Connection failed: {e}")


async def check_calcom() -> CheckResult:
    """Cal.com v2 API check (v1 was decommissioned April 2026)."""
    import time
    key = os.getenv("CALCOM_API_KEY", "")
    if not key:
        return CheckResult("Cal.com", False, "CALCOM_API_KEY missing.")
    try:
        import httpx
        start = time.time()
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                "https://api.cal.com/v2/me",
                headers={
                    "Authorization": f"Bearer {key}",
                    "cal-api-version": "2024-08-13",
                },
            )
        elapsed = (time.time() - start) * 1000
        if r.status_code == 200:
            payload = r.json()
            data = payload.get("data", payload)
            username = data.get("username") or data.get("name") or "unknown"
            email = data.get("email", "n/a")
            return CheckResult("Cal.com", True, f"User '{username}' email={email}", elapsed)
        return CheckResult("Cal.com", False, f"HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        return CheckResult("Cal.com", False, f"Connection failed: {e}")


async def check_vapi() -> CheckResult:
    import time
    key = os.getenv("VAPI_API_KEY", "")
    if not key:
        return CheckResult("Vapi", False, "VAPI_API_KEY missing.")
    try:
        import httpx
        start = time.time()
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                "https://api.vapi.ai/assistant",
                headers={"Authorization": f"Bearer {key}"},
            )
        elapsed = (time.time() - start) * 1000
        if r.status_code == 200:
            data = r.json()
            count = len(data) if isinstance(data, list) else 0
            return CheckResult(
                "Vapi", True,
                f"API key valid; {count} assistant(s) configured.",
                elapsed,
            )
        return CheckResult("Vapi", False, f"HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        return CheckResult("Vapi", False, f"Connection failed: {e}")


async def check_resend() -> CheckResult:
    import time
    key = os.getenv("RESEND_API_KEY", "")
    if not key:
        return CheckResult(
            "Resend", False,
            "RESEND_API_KEY missing — sign up at https://resend.com (free 3,000 emails/month).",
        )
    try:
        import httpx
        start = time.time()
        async with httpx.AsyncClient(timeout=10.0) as client:
            # Try domains endpoint first (full-access keys can read it)
            r = await client.get(
                "https://api.resend.com/domains",
                headers={"Authorization": f"Bearer {key}"},
            )
            elapsed = (time.time() - start) * 1000
            if r.status_code == 200:
                domains = r.json().get("data", [])
                return CheckResult(
                    "Resend", True,
                    f"API key valid (full access); {len(domains)} verified domain(s).",
                    elapsed,
                )
            # Restricted send-only key returns 401 with name=restricted_api_key — that's still valid.
            if r.status_code == 401:
                try:
                    err = r.json()
                except Exception:
                    err = {}
                if err.get("name") == "restricted_api_key":
                    return CheckResult(
                        "Resend", True,
                        "API key valid (send-only restricted key — safe scope).",
                        elapsed,
                    )
                return CheckResult("Resend", False, "401 Unauthorized — key rejected.")
            return CheckResult("Resend", False, f"HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        return CheckResult("Resend", False, f"Connection failed: {e}")


async def check_paddle() -> CheckResult:
    """Live API ping to Paddle."""
    import time
    key = os.getenv("PADDLE_API_KEY", "")
    if not key:
        return CheckResult("Paddle", False, "PADDLE_API_KEY missing.")
    env = os.getenv("PADDLE_ENVIRONMENT", "production").lower()
    base = "https://sandbox-api.paddle.com" if env == "sandbox" else "https://api.paddle.com"
    try:
        import httpx
        start = time.time()
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{base}/event-types",     # cheap read endpoint that confirms auth
                headers={"Authorization": f"Bearer {key}"},
            )
        elapsed = (time.time() - start) * 1000
        if r.status_code == 200:
            data = r.json().get("data", [])
            return CheckResult("Paddle", True, f"API key valid ({env}); {len(data)} event types reachable.", elapsed)
        if r.status_code == 401:
            return CheckResult("Paddle", False, "401 Unauthorized — key rejected.")
        return CheckResult("Paddle", False, f"HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        return CheckResult("Paddle", False, f"Connection failed: {e}")


async def check_billing_provider() -> CheckResult:
    name = os.getenv("BILLING_PROVIDER", "manual")
    if name == "paddle":
        # Delegate to the live Paddle check
        return await check_paddle()
    if name == "manual":
        wise = os.getenv("WISE_PAYMENT_LINK", "")
        if wise:
            return CheckResult("Billing (manual)", True, f"Manual mode — payment link configured: {wise[:50]}...")
        return CheckResult(
            "Billing (manual)", True,
            "Manual mode — no provider needed. Set WISE_PAYMENT_LINK or PAYPAL_PAYMENT_LINK before launch.",
        )
    if name == "polar":
        api = os.getenv("POLAR_API_KEY", "")
        org = os.getenv("POLAR_ORG_ID", "")
        if api and org:
            return CheckResult("Billing (polar)", True, "Polar credentials present (run a real checkout to test fully).")
        return CheckResult("Billing (polar)", False, "POLAR_API_KEY or POLAR_ORG_ID missing.")
    if name == "lemonsqueezy":
        api = os.getenv("LEMONSQUEEZY_API_KEY", "")
        store = os.getenv("LEMONSQUEEZY_STORE_ID", "")
        if api and store:
            return CheckResult("Billing (lemonsqueezy)", True, "Lemon Squeezy credentials present.")
        return CheckResult("Billing (lemonsqueezy)", False, "LEMONSQUEEZY_API_KEY or LEMONSQUEEZY_STORE_ID missing.")
    if name == "coinbase":
        api = os.getenv("COINBASE_COMMERCE_API_KEY", "")
        if api:
            return CheckResult("Billing (coinbase)", True, "Coinbase Commerce key present.")
        return CheckResult("Billing (coinbase)", False, "COINBASE_COMMERCE_API_KEY missing.")
    return CheckResult("Billing", False, f"Unknown BILLING_PROVIDER='{name}'.")


async def check_internal_services() -> list[CheckResult]:
    """In-process checks: tests, manifest, MCP, etc. Async because MCP dispatcher is async."""
    results = []

    # Manifest
    try:
        from agent_interface.manifest_server import get_full_manifest
        ops = get_full_manifest().get("operations", [])
        results.append(CheckResult("Manifest", len(ops) == 12, f"{len(ops)} operations defined."))
    except Exception as e:
        results.append(CheckResult("Manifest", False, f"Failed to load: {e}"))

    # MCP
    try:
        from agent_interface.mcp_server import handle_mcp_request
        r = await handle_mcp_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        )
        tools = r.get("result", {}).get("tools", [])
        results.append(CheckResult("MCP server", len(tools) == 12, f"{len(tools)} tools listed via JSON-RPC."))
    except Exception as e:
        results.append(CheckResult("MCP server", False, f"MCP error: {e}"))

    # Compliance gate
    try:
        from compliance.pre_check import pre_check
        from core.models import ComplianceViolationError
        fired = False
        try:
            pre_check(
                recipient_id="+14045550000", channel="sms", message_type="marketing",
                content="Win big at the casino tonight!", country_code="US",
            )
        except ComplianceViolationError:
            fired = True
        results.append(CheckResult("Compliance gate", fired, "Correctly blocked gambling marketing SMS." if fired else "Did NOT fire — critical."))
    except Exception as e:
        results.append(CheckResult("Compliance gate", False, f"Error: {e}"))

    # Discovery surfaces
    try:
        from agent_interface.well_known import (
            get_ai_plugin_manifest, get_openai_tools, get_anthropic_tools,
            get_agents_json, get_mcp_descriptor, get_llms_txt,
        )
        ok = (
            get_ai_plugin_manifest().get("name_for_model") == "smb_broker"
            and len(get_openai_tools()["tools"]) == 12
            and len(get_anthropic_tools()["tools"]) == 12
            and len(get_agents_json()["skills"]) == 12
            and bool(get_mcp_descriptor()["transport"]["endpoint"])
            and len(get_llms_txt()) > 1000
        )
        results.append(CheckResult("Discovery surfaces", ok, "All 6 .well-known endpoints + llms.txt operational."))
    except Exception as e:
        results.append(CheckResult("Discovery surfaces", False, f"Error: {e}"))

    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main(verbose: bool = False) -> int:
    _load_dotenv()

    print("=" * 70)
    print("SMB BROKER — CREDENTIAL VALIDATION")
    print("=" * 70)
    print()

    print("--- External services ---")
    external = [
        await check_twilio(),
        await check_calcom(),
        await check_vapi(),
        await check_resend(),
        await check_billing_provider(),
    ]
    for r in external:
        icon = "OK  " if r.ok else "FAIL"
        latency = f" ({r.latency_ms:.0f}ms)" if r.latency_ms else ""
        print(f"  [{icon}] {r.name:24} {r.message}{latency}")

    print()
    print("--- Internal services ---")
    internal = await check_internal_services()
    for r in internal:
        icon = "OK  " if r.ok else "FAIL"
        print(f"  [{icon}] {r.name:24} {r.message}")

    print()
    all_results = external + internal
    passed = sum(1 for r in all_results if r.ok)
    total = len(all_results)
    print(f"Summary: {passed}/{total} checks passing.")

    if passed == total:
        print()
        print("All systems operational. Ready to deploy.")
        return 0
    else:
        print()
        print("Some checks failed. Fix the above and re-run.")
        print("Most failures will be missing API keys — see .env.example.")
        return 1


if __name__ == "__main__":
    verbose = "--verbose" in sys.argv or "-v" in sys.argv
    code = asyncio.run(main(verbose=verbose))
    sys.exit(code)
