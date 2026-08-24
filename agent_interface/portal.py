"""
agent_interface/portal.py -- Customer portal router (Slice 5).

Prefix: /portal. Included in main.py via app.include_router(portal_router).

Pure business logic lives in portal_logic.py (no FastAPI dependency, testable).
This module contains only the FastAPI plumbing and Supabase/Polar I/O.

Auth model: magic-link email login -> signed httpOnly session cookie.

Magic token:
  HMAC-SHA256 over "email|expires_at", secret = PORTAL_SESSION_SECRET
  (falls back to KEY_VERIFY_SECRET / JWT_SIGNING_SECRET). TTL = 900s.
  Single-use: consumed token hash stored in in-memory set + Supabase
  portal_used_tokens table when available.

Session cookie (hl_portal):
  Self-contained signed value: base64url(email|exp).sig
  httpOnly; Secure; SameSite=Lax; Domain=.hatchloop.dev; Path=/; Max-Age=30d
  Verified on every authenticated endpoint.

Security invariants:
- POST /login always returns 200 (no email enumeration).
- Magic link is single-use + short TTL + constant-time HMAC compare.
- Session cookie is signed (forged sessions fail verify).
- API key revealed only on explicit POST /key/reveal (never in /me or /balance).
- No secrets appear in any client response.
"""
from __future__ import annotations

import hashlib as _hashlib
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Cookie, HTTPException, Response
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

from agent_interface.portal_logic import (
    make_magic_token,
    verify_magic_token,
    make_session_cookie as _make_session_value,
    verify_session_cookie,
    token_consume_key,
    mask_key,
    product_id_for_package,
    _LOW_BALANCE_THRESHOLD,
)

logger = logging.getLogger("smb_broker.portal")

router = APIRouter(prefix="/portal", tags=["Portal"])

# In-memory consumed-token store. Keyed by token sig hash. Survives within
# a process; Supabase provides durability across restarts (best-effort).
_used_tokens: set[str] = set()

# Portal public URLs MUST be hatchloop.dev: the /portal-api/* proxy prefix only
# exists on Vercel (hatchloop.dev), and the session cookie is Domain=.hatchloop.dev.
# Do NOT fall back to the origin PUBLIC_BASE_URL (smb-broker.onrender.com) - that
# 404s the callback and breaks the cross-domain cookie. Own env, hatchloop.dev default.
_PUBLIC_URL = os.getenv("PORTAL_BASE_URL", "https://hatchloop.dev")
_SESSION_TTL_S = 30 * 24 * 3600  # 30 days


# ---------------------------------------------------------------------------
# Single-use token helpers
# ---------------------------------------------------------------------------

async def _is_token_used(token: str) -> bool:
    key = token_consume_key(token)
    if key in _used_tokens:
        return True
    try:
        from storage.supabase_client import select_rows
        rows = await select_rows("portal_used_tokens", filters={"token_key": key}, limit=1)
        if rows:
            _used_tokens.add(key)
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


async def _consume_token(token: str) -> None:
    key = token_consume_key(token)
    _used_tokens.add(key)
    try:
        from storage.supabase_client import insert_row
        await insert_row("portal_used_tokens", {
            "token_key": key,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Session cookie
# ---------------------------------------------------------------------------

def _set_session(response: Response, email: str) -> None:
    value = _make_session_value(email)
    response.set_cookie(
        key="hl_portal",
        value=value,
        max_age=_SESSION_TTL_S,
        path="/",
        domain=".hatchloop.dev",
        secure=True,
        httponly=True,
        samesite="lax",
    )


def _require_session(hl_portal: Optional[str]) -> str:
    if not hl_portal:
        raise HTTPException(status_code=401, detail="Not authenticated.")
    email = verify_session_cookie(hl_portal)
    if not email:
        raise HTTPException(status_code=401, detail="Session expired or invalid.")
    return email


# ---------------------------------------------------------------------------
# Supabase helpers
# ---------------------------------------------------------------------------

def _sb_config() -> tuple[str, str]:
    url = os.getenv("SUPABASE_URL", "").rstrip("/")
    key = os.getenv("SUPABASE_SERVICE_KEY", "") or os.getenv("SUPABASE_ANON_KEY", "")
    return url, key


async def _get_account(email: str) -> Optional[dict[str, Any]]:
    try:
        from storage.supabase_client import select_rows
        rows = await select_rows("credit_accounts", filters={"email": email}, limit=1)
        return rows[0] if rows else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("portal._get_account failed email=%s err=%s", email, exc)
        return None


async def _get_transactions(account_id: str, limit: int = 50) -> list[dict]:
    url, key = _sb_config()
    if not url or not key:
        return []
    try:
        import httpx
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(
                f"{url}/rest/v1/credit_ledger",
                headers={"apikey": key, "Authorization": f"Bearer {key}"},
                params={
                    "account_id": f"eq.{account_id}",
                    "order": "created_at.desc",
                    "limit": str(limit),
                },
            )
        if resp.status_code == 200:
            return resp.json() or []
    except Exception as exc:  # noqa: BLE001
        logger.debug("portal._get_transactions failed account=%s err=%s", account_id, exc)
    return []


async def _update_account(account_id: str, updates: dict[str, Any]) -> bool:
    url, key = _sb_config()
    if not url or not key:
        return False
    try:
        import httpx
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.patch(
                f"{url}/rest/v1/credit_accounts",
                headers={
                    "apikey": key,
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                    "Prefer": "return=minimal",
                },
                params={"account_id": f"eq.{account_id}"},
                json=updates,
            )
        return resp.status_code in (200, 204)
    except Exception as exc:  # noqa: BLE001
        logger.warning("portal._update_account failed account=%s err=%s", account_id, exc)
        return False


# ---------------------------------------------------------------------------
# Resend magic-link email
# ---------------------------------------------------------------------------

_VALID_PACKAGES = frozenset({"starter", "growth", "scale"})


async def _send_magic_link(email: str, token: str, package: str = "") -> None:
    resend_key = os.getenv("RESEND_API_KEY", "")
    pkg_suffix = f"&package={package}" if package and package in _VALID_PACKAGES else ""
    callback_url = f"{_PUBLIC_URL}/portal-api/callback?token={token}{pkg_suffix}"

    if not resend_key:
        logger.info("DEV magic link (no RESEND_API_KEY): %s", callback_url)
        return

    html = (
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<title>Sign in to HatchLoop</title>"
        "<style>"
        "body{margin:0;padding:0;background:#f9fafb;font-family:system-ui,sans-serif;color:#18181b;}"
        ".wrap{max-width:560px;margin:40px auto;background:#fff;border-radius:12px;"
        "border:1px solid #e4e4e7;padding:40px;}"
        "h1{font-size:22px;font-weight:700;letter-spacing:-0.03em;margin:0 0 12px;}"
        "p{font-size:15px;line-height:1.6;color:#52525b;margin:0 0 24px;}"
        ".btn{display:inline-block;background:#34d399;color:#18181b;font-size:15px;font-weight:700;"
        "padding:13px 32px;border-radius:9999px;text-decoration:none;}"
        ".footer{font-size:12px;color:#a1a1aa;margin-top:24px;}"
        "</style></head><body>"
        "<div class=\"wrap\">"
        "<h1>Sign in to HatchLoop</h1>"
        "<p>Click the button below to access your portal. "
        "This link expires in 15 minutes and can only be used once.</p>"
        f"<a class=\"btn\" href=\"{callback_url}\">Sign in to your portal</a>"
        "<div class=\"footer\">"
        "<p>If you didn&rsquo;t request this, you can safely ignore it.</p>"
        "</div></div></body></html>"
    )
    try:
        import httpx
        async with httpx.AsyncClient(timeout=12.0) as client:
            resp = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {resend_key}", "Content-Type": "application/json"},
                json={
                    "from": "HatchLoop <hello@hatchloop.dev>",
                    "to": [email],
                    "subject": "Sign in to your HatchLoop portal",
                    "html": html,
                },
            )
        if resp.status_code not in (200, 201):
            logger.warning("portal magic link email failed status=%s", resp.status_code)
    except Exception as exc:  # noqa: BLE001
        logger.warning("_send_magic_link exception: %s", exc)


# ---------------------------------------------------------------------------
# Polar helpers
# ---------------------------------------------------------------------------

async def _create_polar_checkout(
    product_id: str, account_id: str, email: str, credits: int,
) -> Optional[str]:
    access_token = os.getenv("POLAR_ACCESS_TOKEN") or os.getenv("POLAR_API_KEY", "")
    if not access_token:
        return None
    try:
        import httpx
        async with httpx.AsyncClient(timeout=12.0) as client:
            resp = await client.post(
                "https://api.polar.sh/v1/checkouts/",
                headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
                json={
                    "product_id": product_id,
                    "success_url": f"{_PUBLIC_URL}/portal",
                    "customer_email": email,
                    "metadata": {"account_id": account_id, "credits": credits},
                },
            )
        if resp.status_code in (200, 201):
            data = resp.json()
            return data.get("url") or data.get("payment_url")
    except Exception as exc:  # noqa: BLE001
        logger.warning("polar checkout exception: %s", exc)
    return None


async def _polar_invoices_url(email: str) -> Optional[str]:
    access_token = os.getenv("POLAR_ACCESS_TOKEN") or os.getenv("POLAR_API_KEY", "")
    if not access_token:
        return None
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://api.polar.sh/v1/customers/",
                headers={"Authorization": f"Bearer {access_token}"},
                params={"email": email, "limit": 1},
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            items = data.get("items") or data.get("results") or []
            if not items:
                return None
            customer_id = items[0].get("id")
            if not customer_id:
                return None
            portal_resp = await client.post(
                f"https://api.polar.sh/v1/customers/{customer_id}/portal/",
                headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
                json={},
            )
            if portal_resp.status_code in (200, 201):
                return portal_resp.json().get("url")
    except Exception as exc:  # noqa: BLE001
        logger.debug("polar invoices url failed: %s", exc)
    return None


# ---------------------------------------------------------------------------
# Key helpers
# ---------------------------------------------------------------------------

async def _issue_key_for_account(account: dict) -> Optional[dict]:
    try:
        from agent_interface.identity import issue_subscription_token
        customer_id = account.get("customer_id") or account.get("account_id", "").replace("sub_", "", 1)
        plan = account.get("plan", "developer")
        email = account.get("email", "")
        resp = issue_subscription_token(customer_id=customer_id, plan=plan, customer_email=email)
        return {"token": resp.token, "agent_id": resp.agent_id, "expires_at": resp.expires_at}
    except Exception as exc:  # noqa: BLE001
        logger.error("_issue_key_for_account failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Request/response models
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    email: str
    package: Optional[str] = None


class TopupRequest(BaseModel):
    package: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/login")
async def portal_login(req: LoginRequest) -> JSONResponse:
    """Send a magic sign-in link. Always 200 (no email enumeration)."""
    email = req.email.strip().lower()
    package = req.package.strip().lower() if req.package else ""
    if package not in _VALID_PACKAGES:
        package = ""
    if email and "@" in email:
        token, _ = make_magic_token(email)
        import asyncio
        asyncio.create_task(_send_magic_link(email, token, package=package))
    return JSONResponse({"ok": True, "message": "If that email has an account, a sign-in link is on its way."})


@router.get("/callback")
async def portal_callback(token: str = "", package: str = "") -> Response:
    """Verify magic token, set session cookie, 302 to /portal (preserving package intent)."""
    if not token:
        return RedirectResponse(url=f"{_PUBLIC_URL}/portal?error=missing_token", status_code=302)
    email = verify_magic_token(token)
    if not email:
        return RedirectResponse(url=f"{_PUBLIC_URL}/portal?error=invalid_token", status_code=302)
    if await _is_token_used(token):
        return RedirectResponse(url=f"{_PUBLIC_URL}/portal?error=token_used", status_code=302)
    await _consume_token(token)
    # Carry the package intent through — only same-site /portal paths, sanitised values only
    safe_package = package.strip().lower() if package and package.strip().lower() in _VALID_PACKAGES else ""
    redirect_url = f"{_PUBLIC_URL}/portal?package={safe_package}" if safe_package else f"{_PUBLIC_URL}/portal"
    response = RedirectResponse(url=redirect_url, status_code=302)
    _set_session(response, email)
    return response


@router.get("/me")
async def portal_me(hl_portal: Optional[str] = Cookie(None)) -> JSONResponse:
    email = _require_session(hl_portal)
    account = await _get_account(email)
    if not account:
        return JSONResponse({
            "email": email,
            "account_id": None,
            "balance_credits": 0,
            "balance_usd": "0.00",
            "key_masked": None,
            "low_balance": False,
            "has_account": False,
        })
    balance = int(account.get("balance_credits", 0))
    key_token = account.get("key_token") or ""
    return JSONResponse({
        "email": email,
        "account_id": account.get("account_id"),
        "balance_credits": balance,
        "balance_usd": f"{balance / 100:.2f}",
        "key_masked": mask_key(key_token) if key_token else None,
        "low_balance": balance < _LOW_BALANCE_THRESHOLD,
        "has_account": True,
    })


@router.get("/balance")
async def portal_balance(hl_portal: Optional[str] = Cookie(None)) -> JSONResponse:
    email = _require_session(hl_portal)
    account = await _get_account(email)
    balance = int(account.get("balance_credits", 0)) if account else 0
    return JSONResponse({
        "balance_credits": balance,
        "balance_usd": f"{balance / 100:.2f}",
        "low_balance": balance < _LOW_BALANCE_THRESHOLD,
    })


@router.get("/transactions")
async def portal_transactions(
    limit: int = 50,
    hl_portal: Optional[str] = Cookie(None),
) -> JSONResponse:
    email = _require_session(hl_portal)
    account = await _get_account(email)
    if not account:
        return JSONResponse({"transactions": [], "total": 0})
    rows = await _get_transactions(account.get("account_id", ""), limit=min(limit, 100))
    return JSONResponse({"transactions": rows, "total": len(rows)})


@router.post("/topup")
async def portal_topup(
    req: TopupRequest,
    hl_portal: Optional[str] = Cookie(None),
) -> JSONResponse:
    email = _require_session(hl_portal)
    package = req.package.lower()
    if package not in ("starter", "growth", "scale"):
        raise HTTPException(status_code=400, detail=f"Unknown package: {package!r}.")
    pkg_match = product_id_for_package(package)
    if not pkg_match:
        return JSONResponse({"ok": False, "reason": "not_configured"})
    product_id, credits = pkg_match
    account = await _get_account(email)
    account_id = account.get("account_id", f"sub_{email}") if account else f"sub_{email}"
    checkout_url = await _create_polar_checkout(product_id, account_id, email, credits)
    if not checkout_url:
        return JSONResponse({"ok": False, "reason": "not_configured"})
    return JSONResponse({"ok": True, "checkout_url": checkout_url})


@router.get("/invoices")
async def portal_invoices(hl_portal: Optional[str] = Cookie(None)) -> JSONResponse:
    email = _require_session(hl_portal)
    portal_url = await _polar_invoices_url(email)
    if not portal_url:
        return JSONResponse({"ok": False, "reason": "not_configured"})
    return JSONResponse({"ok": True, "url": portal_url})


@router.post("/key/reveal")
async def portal_key_reveal(hl_portal: Optional[str] = Cookie(None)) -> JSONResponse:
    email = _require_session(hl_portal)
    account = await _get_account(email)
    if not account:
        return JSONResponse({"ok": False, "reason": "no_account"})
    key_token = account.get("key_token")
    if not key_token:
        issued = await _issue_key_for_account(account)
        if issued:
            await _update_account(
                account.get("account_id", ""),
                {"key_token": issued["token"], "key_jti": issued.get("agent_id", ""),
                 "updated_at": datetime.now(timezone.utc).isoformat()},
            )
            return JSONResponse({"ok": True, "key": issued["token"]})
        return JSONResponse({"ok": False, "reason": "key_issue_failed"})
    return JSONResponse({"ok": True, "key": key_token})


@router.post("/key/generate")
async def portal_key_generate(hl_portal: Optional[str] = Cookie(None)) -> JSONResponse:
    """
    Mint a free-tier API key for the logged-in user (email-verified, no purchase required).

    If a key already exists on the account, returns {ok: true, already: true}.
    If no key: mints a free key using the same customer_id derivation as /keys/verify
    (free_<sha256(email)[:16]>, budget_cap_usd=0.0, 90-day TTL), stores it on the
    credit_accounts row, and returns {ok: true, generated: true}.

    The raw key is NEVER returned here — call POST /key/reveal to retrieve it once.
    """
    email = _require_session(hl_portal)
    account = await _get_account(email)

    # Key already exists — idempotent no-op
    if account and account.get("key_token"):
        return JSONResponse({"ok": True, "already": True})

    # Mint a free-tier JWT using the same deterministic customer_id as /keys/verify
    customer_id = f"free_{_hashlib.sha256(email.encode()).hexdigest()[:16]}"
    _FREE_KEY_TTL_S = 90 * 86400  # 90 days, same as key_requests.py

    from agent_interface.identity import issue_token, TokenRequest
    token_resp = issue_token(TokenRequest(
        agent_id=customer_id,
        principal_id=customer_id,
        principal_type="human",
        allowed_operations=["*"],
        budget_cap_usd=0.0,   # free tier — no credit spend
        allowed_verticals=["*"],
        ttl_seconds=_FREE_KEY_TTL_S,
    ))

    now_iso = datetime.now(timezone.utc).isoformat()

    if account:
        # Account exists but has no key — update in place
        ok = await _update_account(
            account.get("account_id", ""),
            {
                "key_token": token_resp.token,
                "key_jti": token_resp.agent_id,
                "updated_at": now_iso,
            },
        )
        if not ok:
            logger.error("portal.key_generate update_account failed email=%s", email)
            return JSONResponse({"ok": False, "reason": "key_store_failed"})
    else:
        # No account yet — create one with the free key attached
        try:
            from storage.supabase_client import upsert_row
            await upsert_row(
                "credit_accounts",
                {
                    "account_id": customer_id,
                    "email": email,
                    "balance_credits": 0,
                    "plan": "free",
                    "key_token": token_resp.token,
                    "key_jti": token_resp.agent_id,
                    "created_at": now_iso,
                    "updated_at": now_iso,
                },
                on_conflict="email",
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("portal.key_generate upsert_account failed email=%s err=%s", email, exc)
            return JSONResponse({"ok": False, "reason": "key_store_failed"})

    logger.info("portal.free_key_generated customer_id=%s", customer_id)
    # Raw key intentionally NOT returned — call /key/reveal to retrieve it.
    return JSONResponse({"ok": True, "generated": True})


@router.post("/key/regenerate")
async def portal_key_regenerate(hl_portal: Optional[str] = Cookie(None)) -> JSONResponse:
    email = _require_session(hl_portal)
    account = await _get_account(email)
    if not account:
        return JSONResponse({"ok": False, "reason": "no_account"})
    old_jti = account.get("key_jti")
    if old_jti:
        try:
            from agent_interface import identity as _id
            _id._revoked_jtis.add(old_jti)
        except Exception as exc:  # noqa: BLE001
            logger.warning("key_regen: old jti revoke failed jti=%s err=%s", old_jti, exc)
    issued = await _issue_key_for_account(account)
    if not issued:
        return JSONResponse({"ok": False, "reason": "key_issue_failed"})
    await _update_account(
        account.get("account_id", ""),
        {"key_token": issued["token"], "key_jti": issued.get("agent_id", ""),
         "updated_at": datetime.now(timezone.utc).isoformat()},
    )
    return JSONResponse({"ok": True, "key": issued["token"]})


@router.post("/logout")
async def portal_logout() -> JSONResponse:
    response = JSONResponse({"ok": True})
    response.delete_cookie(
        key="hl_portal", path="/", domain=".hatchloop.dev",
        secure=True, httponly=True, samesite="lax",
    )
    return response
