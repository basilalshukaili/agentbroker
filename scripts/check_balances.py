"""
Weekly balance check — does any service have less than 7 days of credit remaining?

Run:
    python scripts/check_balances.py

Add to your calendar as a fortnightly reminder.
"""
from __future__ import annotations

import asyncio
import os
import sys

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


def _load_dotenv():
    p = os.path.join(_PROJECT_ROOT, ".env")
    if not os.path.exists(p):
        return
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            v = v.strip().strip('"').strip("'")
            if k and v and k.strip() not in os.environ:
                os.environ[k.strip()] = v


async def twilio_balance() -> dict:
    sid = os.getenv("TWILIO_ACCOUNT_SID", "")
    tok = os.getenv("TWILIO_AUTH_TOKEN", "")
    if not sid:
        return {"name": "Twilio", "ok": None, "msg": "Not configured."}
    try:
        import httpx
        async with httpx.AsyncClient(auth=(sid, tok), timeout=10.0) as c:
            r = await c.get(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Balance.json")
        if r.status_code != 200:
            return {"name": "Twilio", "ok": False, "msg": f"HTTP {r.status_code}"}
        bal = float(r.json().get("balance", 0))
        # Healthy threshold: $2 = ~250 SMS or ~10 mins voice
        ok = bal > 2.0
        return {
            "name": "Twilio",
            "ok": ok,
            "msg": f"${bal:.2f} remaining" + ("" if ok else " — LOW. Consider email-first fallback."),
            "value": bal,
        }
    except Exception as e:
        return {"name": "Twilio", "ok": False, "msg": f"Error: {e}"}


async def vapi_dashboard_hint() -> dict:
    # Vapi doesn't expose remaining-credit publicly via API as of v1.
    return {
        "name": "Vapi",
        "ok": None,
        "msg": "Check manually at https://dashboard.vapi.ai/account (no API for credit balance).",
    }


async def resend_dashboard_hint() -> dict:
    # Resend has /domains and /emails endpoints but no public quota endpoint.
    return {
        "name": "Resend",
        "ok": None,
        "msg": "Check manually at https://resend.com/dashboard. Free tier: 3,000/month, 100/day.",
    }


async def fly_dashboard_hint() -> dict:
    return {
        "name": "Fly.io",
        "ok": None,
        "msg": "Check at https://fly.io/dashboard/personal — alerts arrive at 80% of free credit.",
    }


async def main() -> int:
    _load_dotenv()
    print("=" * 64)
    print(" SERVICE BALANCE CHECK ")
    print("=" * 64)
    print()

    checks = await asyncio.gather(
        twilio_balance(),
        vapi_dashboard_hint(),
        resend_dashboard_hint(),
        fly_dashboard_hint(),
    )
    any_low = False
    for c in checks:
        icon = "OK  " if c["ok"] is True else ("LOW " if c["ok"] is False else "    ")
        print(f"  [{icon}] {c['name']:10} {c['msg']}")
        if c["ok"] is False:
            any_low = True

    print()
    if any_low:
        print("[WARN] At least one service is low. See EXPIRY_CHECKLIST.md for free alternatives.")
        return 1
    print("[OK] All checked services healthy.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
