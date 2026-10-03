"""
Pure business logic for the email-verified free API key flow.
No FastAPI imports — safe to test without the full web stack.

See key_requests.py for the FastAPI router that calls these functions.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("smb_broker.key_request_logic")

# ---------------------------------------------------------------------------
# Signing secret for verification tokens (distinct from JWT signing secret)
# ---------------------------------------------------------------------------
_VERIFY_SECRET = os.getenv(
    "KEY_VERIFY_SECRET",
    os.getenv("JWT_SIGNING_SECRET", "dev-verify-secret-replace"),
)
_TOKEN_TTL_S = 3600  # verification link valid for 1 hour

FREE_TIER_DAILY_LIMIT = int(os.getenv("FREE_TIER_DAILY_OPS", "100"))
_FREE_TIER_TTL_DAYS = 90

# ---------------------------------------------------------------------------
# In-memory per-key daily rate-limit counters
# {key_id: {"count": int, "date": str(YYYY-MM-DD)}}
# ---------------------------------------------------------------------------
_free_key_daily: dict[str, dict] = {}


def get_free_daily_remaining(key_id: str) -> int:
    """Return how many gated ops remain today for this free key."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    entry = _free_key_daily.get(key_id)
    if not entry or entry.get("date") != today:
        return FREE_TIER_DAILY_LIMIT
    return max(0, FREE_TIER_DAILY_LIMIT - entry.get("count", 0))


def consume_free_daily(key_id: str) -> bool:
    """
    Attempt to consume one free-tier op for key_id.
    Returns True if allowed, False if daily limit exceeded.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    entry = _free_key_daily.get(key_id)
    if not entry or entry.get("date") != today:
        _free_key_daily[key_id] = {"count": 1, "date": today}
        return True
    if entry["count"] >= FREE_TIER_DAILY_LIMIT:
        return False
    entry["count"] += 1
    return True


def is_free_key(key_id: Optional[str]) -> bool:
    """True if this key_id was minted as a free-tier key."""
    return bool(key_id and key_id.startswith("free_"))


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------

def make_verify_token(email: str) -> tuple[str, float]:
    """Generate a signed verification token for `email`. Returns (token, expires_at)."""
    expires_at = time.time() + _TOKEN_TTL_S
    payload = f"{email}|{expires_at}"
    sig = hmac.new(
        _VERIFY_SECRET.encode(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()
    import base64
    b64 = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    return f"{b64}.{sig}", expires_at


def verify_token(token: str) -> Optional[str]:
    """
    Verify a signed token. Returns the email if valid and unexpired, else None.
    """
    import base64
    try:
        parts = token.split(".")
        if len(parts) != 2:
            return None
        b64, sig = parts
        padded = b64 + "=" * (-len(b64) % 4)
        payload = base64.urlsafe_b64decode(padded).decode()
        expected_sig = hmac.new(
            _VERIFY_SECRET.encode(),
            payload.encode(),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(sig, expected_sig):
            return None
        email, exp_str = payload.rsplit("|", 1)
        if time.time() > float(exp_str):
            return None
        return email.strip()
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Resend email helpers
# ---------------------------------------------------------------------------

async def send_verification_email(email: str, verify_url: str) -> bool:
    """Send the verification link via Resend.

    Never RAISES — the caller must not 500 just because a mail provider is
    unhappy — but it DOES tell the truth about what happened, by returning
    whether the email was actually accepted for delivery. Every path that
    does not end in a 2xx from Resend returns False:

      * RESEND_API_KEY unset (the production default today)
      * Resend rejects the send (bad key, suspended account, invalid payload)
      * the request to Resend itself fails (network, timeout, DNS, ...)

    This used to return None unconditionally, so `request_free_key` could not
    tell "sent" from "silently skipped" and told every caller "verification_sent"
    either way — a 200 that looks like success when nothing left this process.
    """
    resend_key = os.getenv("RESEND_API_KEY", "")
    if not resend_key:
        logger.warning("RESEND_API_KEY not set — skipping verification email to %s", email)
        return False
    try:
        import httpx
        payload = {
            "from": "AgentBroker <hello@hatchloop.dev>",
            "to": [email],
            "subject": "Your AgentBroker free API key — verify your email",
            "html": (
                f"<p>Hi,</p>"
                f"<p>Click the link below to verify your email and get your free AgentBroker API key "
                f"(100 gated operations per day).</p>"
                f"<p><a href=\"{verify_url}\">{verify_url}</a></p>"
                f"<p>This link expires in 1 hour.</p>"
                f"<p>If you did not request this, ignore this email.</p>"
                f"<p>&#8212; HatchLoop / AgentBroker</p>"
            ),
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {resend_key}", "Content-Type": "application/json"},
                json=payload,
            )
        if resp.status_code not in (200, 201):
            logger.warning(
                "resend_send_failed email=%s status=%s body=%s",
                email, resp.status_code, resp.text[:200],
            )
            return False
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("resend_exception email=%s err=%s", email, exc)
        return False


async def send_key_email(email: str, token_value: str, expires_iso: str) -> None:
    """Email the minted key to the user. Best-effort — never raises."""
    resend_key = os.getenv("RESEND_API_KEY", "")
    if not resend_key:
        logger.warning("RESEND_API_KEY not set — skipping key delivery email to %s", email)
        return
    paid_url = os.getenv("POLAR_CHECKOUT_URL", "https://buy.polar.sh")
    try:
        import httpx
        payload = {
            "from": "AgentBroker <hello@hatchloop.dev>",
            "to": [email],
            "subject": "Your AgentBroker free API key",
            "html": (
                f"<p>Hi,</p>"
                f"<p>Your free AgentBroker API key is ready:</p>"
                f"<pre style=\"background:#f4f4f4;padding:12px;\">{token_value}</pre>"
                f"<p>Limits: <strong>100 gated operations per day</strong>, "
                f"valid until <strong>{expires_iso}</strong>.</p>"
                f"<p>Usage: send it as the <code>X-Agent-Identity</code> header on every call to "
                f"<code>https://hatchloop.dev/mcp/agent-broker</code>.</p>"
                f"<p>Need more ops? <a href=\"{paid_url}\">Upgrade to paid plan</a>.</p>"
                f"<p>&#8212; HatchLoop / AgentBroker</p>"
            ),
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {resend_key}", "Content-Type": "application/json"},
                json=payload,
            )
        if resp.status_code not in (200, 201):
            logger.warning("key_email_failed email=%s status=%s", email, resp.status_code)
    except Exception as exc:  # noqa: BLE001
        logger.warning("key_email_exception email=%s err=%s", email, exc)


# ---------------------------------------------------------------------------
# Durable pending_keys helpers
# ---------------------------------------------------------------------------

#
# THE TRANSPORT IS A SECURITY DEFINER RPC, NOT THE TABLE (key-holder audit fix 5, 2026-10-01).
# This container holds only the `anon` JWT, which does not bypass RLS, and pending_keys has RLS with
# no policy: the old direct upsert was refused on every call, store_pending swallowed the refusal,
# and consume_pending could never prove a row absent (it read "empty" for rows it was not allowed
# to see), so single use was silently off for every link. migrations/spine/009 adds
# pending_keys_upsert / pending_keys_consume, which run as the table owner. The consume is a
# single DELETE ... RETURNING, so two clicks on one link cannot both win.

async def store_pending(email: str, token: str, expires_at: float) -> bool:
    """Upsert the pending verification row. True when it is stored OR when no database is
    configured at all (local dev, tests: in-process state is the whole picture there);
    False when a configured database refused or did not answer.

    The caller must not send a verification link for a row that was not stored: now that consume
    can prove a row absent, such a link would be refused as 'already used' on its first click."""
    try:
        from storage.supabase_client import _get_config, rpc
        url, key = _get_config()
        if not url or not key:
            return True
        out = await rpc("pending_keys_upsert", {
            "p_email": email,
            "p_token": token,
            "p_expires_at": datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat(),
            "p_created_at": datetime.now(timezone.utc).isoformat(),
        })
        if isinstance(out, dict) and out.get("stored") is True:
            return True
        logger.warning("pending_keys_store_failed email=%s err=unexpected_rpc_shape", email)
        return False
    except Exception as exc:  # noqa: BLE001
        logger.warning("pending_keys_store_failed email=%s err=%s", email, exc)
        return False


class PendingLookupUnavailable(RuntimeError):
    """The pending_keys table could not be read - NOT the same as 'no row'."""


async def consume_pending(token: str, email: Optional[str] = None) -> Optional[str]:
    """
    Look up and delete the pending_key row for this verification, and say
    which of three things happened:
      * the email, if a row was there (first use - go ahead)
      * None, if the row is genuinely absent (already used - refuse)
      * PendingLookupUnavailable, if we could not find out (fall back)

    KEYED ON EMAIL, NOT ON THE TOKEN.

    `store_pending` upserts with `on_conflict="email"`, so there is at most ONE
    row per address no matter how many times someone asks for a link. Looking
    the row up by TOKEN therefore breaks the resend flow, and measurement
    against production showed the conflicting write is a no-op - the row keeps
    the FIRST token:

        request a key, then click "resend"
          first email's link  -> 200, key issued
          second email's link -> 400 "already used"   <- never used

    The second email is the one people click. Whichever way the upsert
    resolves, one of the two live links is refused, and the refusal text tells
    the person a key was already issued when none was.

    The row is per-EMAIL, so the consume has to be per-email too. The token is
    still what proves the request is genuine - `verify_token` checks the HMAC
    and the expiry before this is ever called; this decides only whether that
    verification has already been spent.

    THE TWO USED TO BE THE SAME ANSWER, and they mean opposite things. This
    returned None both for "this link was already used" and for "Supabase did
    not answer", which is why the caller could not enforce single use without
    also breaking signup during a database blip. Verification links are now
    single-use when we can tell, and fall back to signature-only when we
    genuinely cannot - see verify_free_key.
    """
    # ONE ATOMIC CALL, AUTHORITATIVE BOTH WAYS (migrations/spine/009 pending_keys_consume).
    #
    # The previous version read the table (200 [] for "no such row" AND for "a row RLS will not show
    # you"), probed whether the table looked empty to tell the two apart - which also made a
    # genuinely empty table read as "unavailable" - then deleted in a second request whose failure
    # was only logged. The function runs as the table owner, so `found: false` means no row and
    # nothing else, and the delete happens in the same statement as the lookup.
    #
    # What still raises PendingLookupUnavailable (the caller then falls back to the signature,
    # because availability beats a replayed link during an outage): no database configured, a
    # transport or HTTP error, a missing function, or an answer that is not the documented shape.
    payload = {"p_email": email} if email else {"p_token": token}
    try:
        from storage.supabase_client import rpc
        try:
            out = await rpc("pending_keys_consume", payload)
        except RuntimeError as exc:
            raise PendingLookupUnavailable(str(exc)) from exc
        if not isinstance(out, dict) or not isinstance(out.get("found"), bool):
            raise PendingLookupUnavailable(
                "pending_keys_consume returned an unexpected shape - refusing to read it as "
                "'already used'")
        if not out["found"]:
            return None
        return out.get("email") or email
    except PendingLookupUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("pending_keys_consume_failed err=%s", exc)
        raise PendingLookupUnavailable(str(exc)) from exc


# ---------------------------------------------------------------------------
# Machine-mintable key helpers (agent self-serve, no email required)
# ---------------------------------------------------------------------------

# The environment variable is PUBLIC in that we document it to AI builders
# who will embed it in their agent configs. It is still a secret in the
# operational sense — callers who do NOT have it cannot forge a signature.
_MACHINE_MINT_SECRET = os.getenv("MACHINE_MINT_SECRET", "")

# Clock skew budget: reject a timestamp more than this many seconds old
# (or in the future). Prevents replay attacks while tolerating slow networks.
_MINT_FRESHNESS_S = 60

# TTL for machine-minted keys — same as the human free tier.
_MACHINE_MINT_TTL_DAYS = 90


def verify_machine_signature(
    agent_id: str,
    timestamp: int,
    nonce: str,
    signature: str,
) -> tuple[bool, str]:
    """
    Verify a machine-mint request.

    Returns (ok: bool, reason: str).  Always returns a generic reason on
    failure so callers cannot distinguish "wrong secret" from "bad clock" and
    cannot time the guess.

    Signature spec (from task brief, immutable):
      HMAC-SHA256(agent_id + str(timestamp) + nonce, MACHINE_MINT_SECRET)
    where the HMAC input is the raw concatenation (no separators) of the three
    fields and the digest is lowercased hex.
    """
    if not _MACHINE_MINT_SECRET:
        return False, "not_configured"

    # 1. Freshness: reject stale or future timestamps
    now = int(time.time())
    age = now - timestamp
    if abs(age) > _MINT_FRESHNESS_S:
        return False, "invalid_request"

    # 2. Signature
    message = (agent_id + str(timestamp) + nonce).encode()
    expected = hmac.new(
        _MACHINE_MINT_SECRET.encode(),
        message,
        hashlib.sha256,
    ).hexdigest()
    try:
        if not hmac.compare_digest(expected, signature.lower()):
            return False, "invalid_request"
    except Exception:  # noqa: BLE001
        return False, "invalid_request"

    return True, "ok"


async def store_machine_minted(
    agent_id: str,
    token_value: str,
    expires_at: float,
) -> None:
    """
    Record the minted key in pending_keys with source='machine_minted'.
    Best-effort — never raises. The key is already issued before this runs;
    a storage hiccup must not break the caller's response.

    The pending_keys table is shared with the email flow. The `source` column
    distinguishes machine-minted rows from human email-verified ones. The
    migration `machine_mintable_key.sql` adds that column.
    """
    try:
        from storage.supabase_client import _get_config, rpc
        url, key = _get_config()
        if not url or not key:
            return
        await rpc("pending_keys_upsert", {
            "p_email": f"agent:{agent_id}",   # surrogate — no real email
            "p_token": token_value[:512],       # store a prefix; full JWT is long
            "p_expires_at": datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat(),
            "p_created_at": datetime.now(timezone.utc).isoformat(),
            "p_source": "machine_minted",
        })
    except Exception as exc:  # noqa: BLE001
        logger.warning("machine_minted_store_failed agent_id=%s err=%s", agent_id, exc)


# ---------------------------------------------------------------------------
# HTML templates
# ---------------------------------------------------------------------------

def html_error(title: str, body_html: str) -> str:
    return (
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<title>{title}</title>"
        "<style>body{font-family:system-ui,sans-serif;max-width:600px;margin:60px auto;padding:0 20px;}"
        "h1{color:#c0392b;} a{color:#2980b9;}</style></head><body>"
        f"<h1>{title}</h1><p>{body_html}</p></body></html>"
    )


def html_success(token_value: str, expires_iso: str, key_id: str, paid_url: str) -> str:
    # "Need more? Buy credits" is shown only while the credits gate runs (billing.switches). It was shown to
    # every new free-key holder while CREDITS_ENABLED was off: a package bought then mints a key that is
    # never credited. With the gate off there is nothing to offer, so the block is not there at all.
    from billing import switches
    _need_more = (
        "<h2>Need more?</h2>"
        "<p><a href=\"https://hatchloop.dev/pricing\">Buy credits</a> &#8212; Starter $9/1,000 ops, Growth $29/3,500, "
        "Scale $99/13,000. No flat or unlimited subscription.</p>"
        if switches.credits_enabled() else "")
    return (
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<title>Your AgentBroker Free Key</title>"
        "<style>"
        "body{font-family:system-ui,sans-serif;max-width:640px;margin:60px auto;padding:0 20px;}"
        "h1{color:#27ae60;} pre{background:#f4f4f4;padding:16px;overflow-x:auto;font-size:13px;word-break:break-all;}"
        ".box{border:1px solid #ddd;border-radius:6px;padding:20px;margin:20px 0;}"
        "a{color:#2980b9;} .badge{display:inline-block;background:#27ae60;color:#fff;border-radius:4px;padding:2px 8px;font-size:12px;}"
        "</style></head><body>"
        "<h1>Your free API key is ready <span class=\"badge\">FREE TIER</span></h1>"
        "<div class=\"box\">"
        f"<p><strong>Key ID:</strong> <code>{key_id}</code></p>"
        "<p><strong>API Key</strong> (copy and keep safe &#8212; not shown again):</p>"
        f"<pre>{token_value}</pre>"
        f"<p><strong>Expires:</strong> {expires_iso} &nbsp;&bull;&nbsp;"
        "<strong>Limit:</strong> 100 gated operations per day</p>"
        "</div>"
        "<h2>How to use</h2>"
        "<p>Add this header to every MCP call:</p>"
        f"<pre>X-Agent-Identity: {token_value}</pre>"
        "<p>Endpoint: <code>https://hatchloop.dev/mcp/agent-broker</code></p>"
        + _need_more +
        "<p style=\"color:#999;font-size:12px;\">Your key was also sent to your email.</p>"
        "</body></html>"
    )


async def handle_mint_key_mcp(
    agent_id: str = "",
    timestamp: object = 0,
    nonce: str = "",
    signature: str = "",
) -> dict:
    """MCP dispatch wrapper for machine_mint_key.

    The HTTP handler (key_requests.py) returns FastAPI JSONResponse objects;
    the MCP dispatcher expects plain dicts.  This function has the same logic
    but returns a receipt dict so it can be called from _h_tools_call.
    """
    agent_id = str(agent_id).strip()
    try:
        timestamp = int(timestamp)
    except (TypeError, ValueError):
        return {"status": "failure", "error": "invalid_request",
                "detail": "timestamp must be an integer Unix epoch."}
    nonce = str(nonce)
    signature = str(signature)

    ok, reason = verify_machine_signature(agent_id, timestamp, nonce, signature)
    if not ok:
        if reason == "not_configured":
            return {"status": "failure", "error": "not_configured",
                    "detail": ("MACHINE_MINT_SECRET is not set on this server. "
                               "The feature is deployed but not yet activated.")}
        return {"status": "failure", "error": "invalid_request",
                "detail": ("Signature verification failed. "
                           "Check that your timestamp is within 60s of server time, "
                           "your nonce is unique, and your HMAC key is correct.")}

    safe_id = agent_id[:200]
    customer_id = f"free_machine_{hashlib.sha256(safe_id.encode()).hexdigest()[:16]}"
    ttl_seconds = _FREE_TIER_TTL_DAYS * 86400

    from agent_interface.identity import issue_token, TokenRequest
    token_resp = issue_token(TokenRequest(
        agent_id=customer_id,
        principal_id=customer_id,
        principal_type="system",
        allowed_operations=["*"],
        budget_cap_usd=0.0,
        allowed_verticals=["*"],
        ttl_seconds=ttl_seconds,
    ))
    token_value = token_resp.token
    expires_iso = datetime.fromtimestamp(
        token_resp.expires_at, tz=timezone.utc
    ).strftime("%Y-%m-%d")

    await store_machine_minted(safe_id, token_value, token_resp.expires_at)

    logger.info(
        "machine_key_issued_via_mcp customer_id=%s agent_id_hash=%s",
        customer_id, hashlib.sha256(safe_id.encode()).hexdigest()[:8],
    )

    return {
        "status": "success",
        "ok": True,
        "key": token_value,
        "key_id": customer_id,
        "expires_at": expires_iso,
        "tier": "free",
        "daily_limit": 100,
        "usage": ("Send as the X-Agent-Identity header on every call to "
                  "https://hatchloop.dev/mcp/agent-broker"),
    }
