"""
billing/anon_trial.py -- the find_business free trial: a stranger's first
few calls need no key, and after that a (free) key.

WHAT THIS IS
    A caller with NO valid key gets core.tool_auth.TRIAL_CALLS_PER_CALLER
    successful calls to each tool in core.tool_auth.TRIAL_TOOLS (today only
    find_business). Call N+1 is not an opaque error: it is a normal MCP tool
    RESULT (isError: true) that says, in plain words, that the trial is used
    up and exactly how to get a key. A caller who presents a valid key is never
    counted and never limited - keyed and paid behaviour is unchanged.

    Founder scope, chat 4 row 3070 ("Yes proceed", answering row 3068): "make
    find_business a zero-friction free trial (no signup, 10 free calls) so any
    Smithery visitor can test it immediately." That scope and nothing wider:
    no other tool's pricing or access is touched by this module.

THE FOUR PROPERTIES THIS MODULE EXISTS TO HAVE

  1. DURABLE. Counts live in Supabase (the `anon_data_quota` table, through the
     SECURITY DEFINER RPCs in sql/agentbroker/008_anon_trial_reserve_release_
     rpc.sql), never in process memory. A container restart or a redeploy - the
     deploy swaps in a brand new container - must not hand every caller a fresh
     allowance. This is the same store and the same anon-key/RPC architecture
     billing/data_quota.py already uses for the anonymous premium-data quota.

  2. FAILS CLOSED. If the counter cannot be reached, or answers with a shape we
     did not ask for, the caller gets the "get a key" response and the tool is
     NOT run. billing/data_quota.py fails OPEN, on purpose, because its premium
     tools are metered convenience; here the counter is the only thing between
     a free trial and unbounded anonymous use, so an outage must not become an
     open tap. The cost of failing closed is that a Supabase outage makes the
     KEYLESS find_business path answer "get a key" - keyed calls never touch the
     counter and are unaffected.

  3. BOUNDED GLOBALLY. Besides the per-caller allowance there is a service-wide
     ceiling on anonymous calls per UTC day (config.FIND_BUSINESS_TRIAL_GLOBAL_
     DAILY). Allowances are per caller, and a caller who rotates IP addresses
     collects a fresh one each time; the ceiling is what stops that turning a
     free trial into unbounded upstream cost. Both counters are checked and
     incremented in ONE atomic database call, so the ceiling cannot be
     overshot by concurrent requests and a refused request consumes nothing.

  4. ONLY SUCCESS COUNTS. The slot is reserved before the tool runs (the only
     way to enforce the limit under concurrency) and RELEASED if the tool
     raises or returns status=failure. A call that failed argument validation
     therefore costs the caller nothing. If the release itself cannot be
     reached the caller keeps the loss - conservative, logged at ERROR.
     Discovery traffic (initialize, tools/list, resources/*, prompts/*, ping)
     never reaches this module at all: only a tools/call of a trial tool does.

WHO IS "THE SAME CALLER" -- and why this is not trivially spoofable
    The caller identity is the client IP as seen through the reverse proxy,
    hashed with a server-side secret (HMAC-SHA256), never stored raw. Two
    decisions here are security decisions and are argued where they are made:

      * Forwarding headers are trusted ONLY when the TCP peer is our own local
        proxy. The origin is published to 127.0.0.1 only (Caddy is the one way
        in), so a peer that is not loopback/private is somebody reaching the
        process directly, and for them X-Forwarded-For is just a header they
        wrote. main.py stamps the peer address into `x-hl-peer-addr` itself,
        overwriting anything the client sent under that name.
      * Of the forwarded chain we take the RIGHTMOST entry that is not a local
        proxy - the address our own proxy appended - not the leftmost, which is
        whatever the client claimed. (Caddy with no trusted_proxies configured
        overwrites the header with the connecting address, so today the chain
        has one entry and the two agree; the rightmost rule is the one that
        stays correct if a CDN is ever put in front and appends.)
      * IPv6 callers are grouped by /64. A single home or cloud tenant is
        handed a whole /64, so keying on the full address would let one machine
        mint 2^64 "different" callers.
      * X-Real-IP is deliberately ignored: nothing in front of us sets it, so
        any value is client-supplied.

    THE SECOND PROXY HOP, which is the reason ANON_TRIAL_TRUSTED_PROXIES
    exists. The URL the registries publish, hatchloop.dev/mcp/agent-broker, is
    NOT served by this container's Caddy: it is a Next.js rewrite on the same
    box (web_hatchloop_v2/next.config.ts) that makes a server-side request to
    https://api.hatchloop.dev/mcp. Caddy sees that request arrive from the
    box's own public address, does not trust it, and overwrites
    X-Forwarded-For with it - so every visitor who came in through the
    canonical URL looks like ONE caller and shares ONE allowance. Until Caddy
    is told to trust the box's own address (global `servers { trusted_proxies
    static <box ip> }`, so the visitor's address survives the hop) AND that
    address is listed here, the per-caller limit is not per visitor on that
    URL. Both are operator actions outside this repository; see the report.
    api.hatchloop.dev/mcp (what smithery.yaml points at) has no such hop.

    What this does NOT do, stated so nobody assumes it does: it cannot tell
    apart two people behind one NAT (they share one allowance), and it cannot
    stop someone with many real IP addresses - that is what the global daily
    ceiling is for. It is a friction limit for an honest trial, not identity
    verification. The User-Agent is NOT consulted: the crawler classification
    in billing/usage_logger.py keys on it, and exempting "crawlers" from the
    limit would let anyone write `curl/` and be unlimited.

DEPLOY PREREQUISITE (fails closed if missing)
    sql/agentbroker/008_anon_trial_reserve_release_rpc.sql must be APPLIED to
    the Supabase project before a build containing this module goes live. If
    the functions are absent the reserve call gets a 404 from PostgREST, the
    trial fails closed, and every keyless find_business call answers "get a
    key". That is the intended safe direction - and a self-inflicted outage of
    the keyless path. tests/integration/test_anon_trial_rpc_live.py is the
    operator check.
"""
from __future__ import annotations

import asyncio
import functools
import hashlib
import hmac
import ipaddress
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from core.tool_auth import TRIAL_CALLS_PER_CALLER, TRIAL_TOOLS

log = logging.getLogger("smb_broker.anon_trial")

# Stamped by main.py from the socket's peer address. NEVER read from a client:
# main.py overwrites whatever arrived under this name before calling us.
PEER_HEADER = "x-hl-peer-addr"

# A Supabase RPC that has not answered in this long is treated as unreachable.
# Two seconds matches billing/data_quota.py's budget for the same store.
_RPC_TIMEOUT_S = 2.0

# The same development default identity.py/key_request_logic.py fall back to.
# Only ever used when JWT_SIGNING_SECRET is not set, i.e. never in production.
_DEV_SECRET = "dev-verify-secret-replace"

# Addresses of our OWN reverse proxy and container network. A TCP peer in one
# of these is Caddy (or a sibling process on the box), so what it says in
# X-Forwarded-For was written by our proxy. Anything else is a direct caller.
_LOCAL_PROXY_NETS = tuple(ipaddress.ip_network(n) for n in (
    "127.0.0.0/8", "::1/128",                    # loopback
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",   # RFC 1918 (docker bridge)
    "169.254.0.0/16", "fe80::/10",               # link-local
    "fc00::/7",                                  # IPv6 unique-local
))

# Additional proxy addresses/networks whose X-Forwarded-For entries are OUR OWN
# and are skipped when looking for the client (comma separated). The default is
# non-empty on purpose: scripts/check_deploy_env.py treats a variable with an
# empty default as one the container MUST be given, and this one is optional.
_EXTRA_PROXIES_ENV = "ANON_TRIAL_TRUSTED_PROXIES"
_EXTRA_PROXIES_DEFAULT = "127.0.0.1"

REASON_CALLER_LIMIT = "caller_limit"
REASON_GLOBAL_LIMIT = "global_limit"
REASON_STORE_UNAVAILABLE = "store_unavailable"


# ---------------------------------------------------------------------------
# Small pure helpers (no I/O -- unit-tested directly)
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _today_utc() -> str:
    return _now().strftime("%Y-%m-%d")


def _seconds_to_utc_midnight() -> int:
    now = _now()
    midnight = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return max(1, int((midnight - now).total_seconds()))


def global_daily_ceiling() -> int:
    """Service-wide anonymous calls per UTC day. Env still wins at call time."""
    try:
        import config
        default = int(config.FIND_BUSINESS_TRIAL_GLOBAL_DAILY)
    except Exception:  # noqa: BLE001 - config must never break the gate
        default = 1000
    try:
        return max(0, int(os.getenv("FIND_BUSINESS_TRIAL_GLOBAL_DAILY", str(default))))
    except ValueError:
        return default


def _parse_ip(value):
    """A single address out of a header value, or None. Never raises."""
    if not value or not isinstance(value, str):
        return None
    v = value.strip()
    if not v:
        return None
    if v.startswith("[") and "]" in v:              # [::1]:443
        v = v[1:v.index("]")]
    elif v.count(":") == 1 and "." in v:            # 1.2.3.4:443
        v = v.rsplit(":", 1)[0]
    v = v.split("%", 1)[0]                          # fe80::1%eth0
    try:
        ip = ipaddress.ip_address(v)
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


@functools.lru_cache(maxsize=8)
def _parse_extra_proxies(raw: str) -> tuple:
    """Operator-configured proxy networks. A malformed entry is dropped and
    logged, never guessed at: an unparseable trust list must not silently
    widen who is believed."""
    nets = []
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            log.error("%s: ignoring %r, which is not an IP address or network",
                      _EXTRA_PROXIES_ENV, part)
    return tuple(nets)


def _is_local_proxy(ip) -> bool:
    """Is this address one of OUR proxies (so its say-so about a client is ours)?"""
    extra = _parse_extra_proxies(os.getenv(_EXTRA_PROXIES_ENV, _EXTRA_PROXIES_DEFAULT))
    return any(ip in net for net in _LOCAL_PROXY_NETS + extra if net.version == ip.version)


def client_ip(headers: dict):
    """The caller's address, or None when it cannot be established honestly.

    None is a real answer, not an error: it means "I could not verify who is
    on the other end", and the caller is then counted under one shared
    'unidentified' identity rather than being given a private allowance.
    """
    peer = _parse_ip(headers.get(PEER_HEADER))
    if peer is None:
        return None
    if not _is_local_proxy(peer):
        # A direct connection. Whatever it put in X-Forwarded-For is its own
        # claim about itself, so it is not consulted.
        return peer
    chain = [h.strip() for h in (headers.get("x-forwarded-for") or "").split(",")
             if h.strip()]
    for hop in reversed(chain):
        ip = _parse_ip(hop)
        if ip is None:
            # Our proxy never writes a malformed hop, so this chain was not
            # produced by it. Refuse to guess.
            return None
        if not _is_local_proxy(ip):
            return ip
    return peer


def caller_identity(headers: dict) -> str:
    """A stable, coarse label for 'the same caller' -- never stored raw."""
    ip = client_ip(headers)
    if ip is None:
        return "unidentified"
    if ip.version == 6:
        net = ipaddress.ip_network((ip, 64), strict=False)
        return "v6:" + str(net.network_address)
    return "v4:" + str(ip)


def _pepper() -> bytes:
    """A server-side secret for hashing identities.

    An IPv4 address has only 2^32 values, so a bare SHA-256 of one is undone by
    trying all of them. Keying the hash with a secret the database never sees
    means a leak of the counter table does not disclose who used the service.
    Derived (HMAC with a fixed purpose label) from a secret the container
    already holds, so no new environment variable is needed and the raw signing
    secret is never used directly as a hash key. Rotating that secret resets
    every caller's allowance, which is an acceptable price for not adding a
    second secret to operate.
    """
    secret = os.getenv("JWT_SIGNING_SECRET", _DEV_SECRET)
    return hmac.new(secret.encode("utf-8"), b"hatchloop/anon-trial/v1",
                    hashlib.sha256).digest()


def caller_key(tool: str, identity: str) -> str:
    """64 hex chars. The only form of a caller that reaches the database."""
    return hmac.new(_pepper(), (tool + "|" + identity).encode("utf-8"),
                    hashlib.sha256).hexdigest()


def _lower(headers: Optional[dict]) -> dict:
    return {str(k).lower(): v for k, v in dict(headers or {}).items()}


def _presented_token(headers: dict) -> str:
    """The credential the caller sent, under any header name we document.

    handle_mcp_request folds `Authorization: Bearer` and `x-api-key` into
    x-agent-identity before tools/call; the REST route does not go through it,
    so the same three names are read here to make both doors agree.
    """
    tok = str(headers.get("x-agent-identity") or "").strip()
    if tok:
        return tok
    auth = str(headers.get("authorization") or "")
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip()
    return str(headers.get("x-api-key") or "").strip()


def _has_valid_key(headers: dict) -> bool:
    token = _presented_token(headers)
    if not token or token == "anonymous":
        return False
    try:
        from agent_interface.identity import validate_token
        result = validate_token(token)
        return bool(result and result.valid)
    except Exception:  # noqa: BLE001
        # A validator that raises has not vouched for this caller. They are
        # counted as keyless; the trial's allowance is what they get.
        return False


# ---------------------------------------------------------------------------
# The store seam. Tests replace _rpc; production talks to Supabase.
# ---------------------------------------------------------------------------

async def _rpc(fn: str, payload: dict):
    """One PostgREST RPC call. Raises on ANY failure (storage.supabase_client
    contract), including a timeout -- the caller decides what failure means."""
    from storage.supabase_client import rpc
    return await asyncio.wait_for(rpc(fn, payload), timeout=_RPC_TIMEOUT_S)


def _verify_reserve_shape(payload, caller_limit: Optional[int] = None) -> dict:
    """Trust nothing about the reply that we did not ask for.

    A 2xx body of the wrong shape (a proxy interstitial, a function that
    changed) must never be read as 'allowed'. Raising here sends the caller
    down the fail-closed path. When the caller's limit is known, an 'allowed'
    reply whose count is outside 1..limit is refused too: a database function
    that admits a call it just counted past the limit is not one to trust.
    """
    if (caller_limit is not None and isinstance(payload, dict)
            and payload.get("allowed") is True
            and isinstance(payload.get("caller_count"), int)
            and not (1 <= payload["caller_count"] <= caller_limit)):
        raise ValueError("anon_trial_reserve admitted a call at count %r with a "
                         "limit of %r" % (payload["caller_count"], caller_limit))
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("allowed"), bool)
        or not isinstance(payload.get("caller_count"), int)
        or isinstance(payload.get("caller_count"), bool)
        or not isinstance(payload.get("global_count"), int)
        or isinstance(payload.get("global_count"), bool)
        or payload.get("reason") not in ("ok", REASON_CALLER_LIMIT, REASON_GLOBAL_LIMIT)
        or (payload["allowed"] and payload["reason"] != "ok")
        or (not payload["allowed"] and payload["reason"] == "ok")
    ):
        raise ValueError("anon_trial_reserve returned an unexpected shape: %r" % (payload,))
    return payload


# ---------------------------------------------------------------------------
# Admission
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Ticket:
    """A reserved slot. Held from admission until the tool's outcome is known."""
    tool: str
    caller_key: str
    day: str
    used: int          # this caller's count INCLUDING this call
    limit: int

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)


@dataclass(frozen=True)
class Admission:
    """What the gate decided.

    outcome:
      not_applicable  the tool is not a trial tool -- nothing to do
      keyed           a valid key was presented -- never counted, never limited
      admitted        a slot was reserved; `ticket` must be settled
      denied          the tool must NOT run; `denial` is the MCP result to return
    """
    outcome: str
    ticket: Optional[Ticket] = None
    reason: Optional[str] = None
    denial: Optional[dict] = None

    @property
    def denied(self) -> bool:
        return self.outcome == "denied"


async def admit(name: str, headers: Optional[dict]) -> Admission:
    """Decide whether this call may run, reserving a slot if it may."""
    if name not in TRIAL_TOOLS:
        return Admission("not_applicable")
    hdrs = _lower(headers)
    if _has_valid_key(hdrs):
        return Admission("keyed")

    key = caller_key(name, caller_identity(hdrs))
    day = _today_utc()
    try:
        verdict = _verify_reserve_shape(await _rpc("anon_trial_reserve", {
            "p_tool": name,
            "p_caller_key": key,
            "p_day": day,
            "p_caller_limit": TRIAL_CALLS_PER_CALLER,
            "p_global_limit": global_daily_ceiling(),
        }), caller_limit=TRIAL_CALLS_PER_CALLER)
    except Exception as exc:  # noqa: BLE001 - every failure is the same decision
        _log_store_failure(exc, key)
        return Admission("denied", reason=REASON_STORE_UNAVAILABLE,
                         denial=denial_result(name, REASON_STORE_UNAVAILABLE))

    if verdict["allowed"]:
        return Admission("admitted", ticket=Ticket(
            tool=name, caller_key=key, day=day,
            used=verdict["caller_count"], limit=TRIAL_CALLS_PER_CALLER))
    return Admission("denied", reason=verdict["reason"],
                     denial=denial_result(name, verdict["reason"]))


async def settle_failure(admission: Admission) -> bool:
    """Give the reserved slot back because the call did not succeed.

    Returns True when the store confirmed the release. Never raises: this runs
    on an error path and must not replace the original error with its own.
    """
    t = admission.ticket
    if admission.outcome != "admitted" or t is None:
        return False
    try:
        out = await _rpc("anon_trial_release", {
            "p_tool": t.tool, "p_caller_key": t.caller_key, "p_day": t.day})
        if isinstance(out, dict) and isinstance(out.get("released"), bool):
            return out["released"]
        raise ValueError("anon_trial_release returned an unexpected shape: %r" % (out,))
    except Exception as exc:  # noqa: BLE001
        log.error("anon_trial_release_failed tool=%s caller=%.8s... err=%s -- the "
                  "caller keeps the loss of one trial call (conservative)",
                  t.tool, t.caller_key, exc)
        return False


def annotate_success(receipt: dict, admission: Admission) -> None:
    """Tell a keyless caller how much of the trial is left. Never raises."""
    t = admission.ticket
    if admission.outcome != "admitted" or t is None or not isinstance(receipt, dict):
        return
    try:
        block = {
            "applies_to": "calls made without a valid key; keyed calls are free "
                          "and are not counted",
            "calls_included": t.limit,
            "calls_used": t.used,
            "calls_remaining": t.remaining,
        }
        if t.remaining == 0:
            block["note"] = (
                "That was your last keyless " + t.tool + " call. It stays free "
                "with a key - get one: " + _key_request_url())
        else:
            block["get_a_key"] = _key_request_url()
        receipt["free_trial"] = block
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _log_store_failure(exc: Exception, key: str) -> None:
    """Loud, and distinguishable: a missing migration is not an outage."""
    kind = "unclassified"
    try:
        from billing.data_quota import _classify_rpc_exception
        kind, _ = _classify_rpc_exception(exc)
    except Exception:  # noqa: BLE001
        pass
    log.error(
        "anon_trial_unavailable kind=%s caller=%.8s... err=%s -- FAILING CLOSED: "
        "this keyless call was refused and the tool was not run. kind=misconfigured "
        "means sql/agentbroker/008_anon_trial_reserve_release_rpc.sql is not "
        "applied (or anon cannot execute it); every keyless call will fail until "
        "it is.", kind, key, exc)


# ---------------------------------------------------------------------------
# The refusal. This is the storefront moment: it must say what happened and
# how to fix it, in the tool RESULT (many MCP clients hide JSON-RPC errors from
# the model entirely -- see _ToolError in mcp_server.py).
# ---------------------------------------------------------------------------

def _public_base() -> str:
    return os.getenv("PUBLIC_BASE_URL", "https://api.hatchloop.dev").rstrip("/")


def _key_request_url() -> str:
    return _public_base() + "/keys/request"


def _how_to_get_a_key(tool: str) -> dict:
    url = _key_request_url()
    return {
        "free_key": {
            "method": "POST",
            "url": url,
            "body": {"email": "you@example.com"},
            "then": ("Open the verification link we email you; it shows your key. "
                     "Send it on every call as the X-Agent-Identity header (or "
                     "Authorization: Bearer <key>)."),
            "note": tool + " is free with a key too - a key is not a payment.",
        },
        "header": "X-Agent-Identity",
    }


def _messages(tool: str, reason: str) -> tuple:
    """(human_message, error_code, retriable) for a refusal."""
    url = _key_request_url()
    n = TRIAL_CALLS_PER_CALLER
    how = ("Get a free key in about a minute: POST " + url + " with your email "
           "in the body (" + '{"email": "you@example.com"}' + "), open the link we "
           "email you, and send the key as the X-Agent-Identity header (or "
           "Authorization: Bearer <key>) on every call. Keyed " + tool + " calls "
           "are free and are not counted against any trial.")
    if reason == REASON_CALLER_LIMIT:
        return ("Your " + str(n) + " free keyless " + tool + " calls are used up. "
                + tool + " is still free - it now just needs a key. " + how
                + " Nothing else changed: the tools marked [free, no key] in "
                "tools/list still need no key.", "auth_required", False)
    if reason == REASON_GLOBAL_LIMIT:
        return ("The keyless " + tool + " trial has reached its service-wide limit "
                "for today (UTC). That is a cap on unauthenticated use across all "
                "callers, not something you did, and it reopens at 00:00 UTC. To "
                "use " + tool + " right now, use a key. " + how, "rate_limited", True)
    return ("We could not check how many keyless " + tool + " calls you have left, "
            "and we do not serve uncounted anonymous calls, so this call was not "
            "run. Retry in a moment, or use a key now - keyed calls do not depend "
            "on the trial counter. " + how, "auth_required", True)


def denial_body(tool: str, reason: str) -> dict:
    message, error_code, retriable = _messages(tool, reason)
    used_up = reason == REASON_CALLER_LIMIT
    body = {
        "status": "failure",
        "reason_code": {
            REASON_CALLER_LIMIT: "free_trial_exhausted",
            REASON_GLOBAL_LIMIT: "free_trial_daily_capacity",
        }.get(reason, "free_trial_unavailable"),
        "error_code": error_code,
        "retriable": retriable,
        "human_message": message,
        "how_to_resolve": _how_to_get_a_key(tool),
        "free_trial": {
            "tool": tool,
            "applies_to": "calls made without a valid key",
            "calls_included": TRIAL_CALLS_PER_CALLER,
            # Only the caller-limit refusal knows the caller's own count; the
            # other two do not, and must not claim a number they did not read.
            **({"calls_remaining": 0} if used_up else {}),
        },
        "cost": {"amount": 0.0, "currency": "USD", "basis": "free"},
    }
    if reason == REASON_GLOBAL_LIMIT:
        body["retry_after_ms"] = _seconds_to_utc_midnight() * 1000
    return body


def denial_result(tool: str, reason: str) -> dict:
    """The refusal as an MCP tools/call result."""
    return {
        "content": [{"type": "text",
                     "text": json.dumps(denial_body(tool, reason), indent=2,
                                        default=str)}],
        "isError": True,
    }


def denial_http(tool: str, reason: str) -> tuple:
    """(status_code, detail, headers) for the REST /ops/<tool> door."""
    body = denial_body(tool, reason)
    if reason == REASON_GLOBAL_LIMIT:
        return 429, body, {"Retry-After": str(_seconds_to_utc_midnight())}
    return 401, body, {}
