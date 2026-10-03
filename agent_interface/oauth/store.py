"""Durable state for the sign-in: the spine in production, a faithful in-memory twin for tests and local dev.

THE SPINE STORE is a thin client over the `oauth_*` functions of migrations/spine/011_oauth_connect.sql.
Every method is one RPC; the database does the atomic part (spend a code, rotate a refresh token, press a
link), so two simultaneous requests cannot both win no matter how many containers there are. The store holds
no logic of its own beyond translating an unreachable database into `StoreUnavailable`, which the endpoints
turn into an honest "try again" - never into an issued token and never into a silent success.

THE MEMORY STORE implements the same contract in-process (one event loop, so each method is atomic by
construction) with an injectable clock. It exists so the endpoints can be tested without a database and so
`uvicorn main:app` works on a laptop with no spine. It is NEVER the production store: `get_store()` returns
it only when a test or a developer put it there explicitly. tests/unit/test_oauth_connect_flow.py runs the
same scenarios against it AND against the real SQL (through this module's SpineStore, with the HTTP hop
replaced by a direct call), so the two cannot drift apart unnoticed.

Nothing here ever sees, stores or logs a raw secret or an email address: callers pass SHA-256 hex digests.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable, Optional

log = logging.getLogger("smb_broker.oauth.store")


class StoreUnavailable(RuntimeError):
    """The database could not be reached or did not answer in the documented shape."""


# ---------------------------------------------------------------------------
# The spine
# ---------------------------------------------------------------------------

class SpineStore:
    def __init__(self, rpc: Optional[Callable] = None) -> None:
        # The transport is injectable so tests can run this exact class against a real PostgreSQL without
        # patching a module attribute other modules have already bound. Production passes nothing.
        self._rpc = rpc

    async def _call(self, fn: str, payload: dict) -> Any:
        try:
            rpc = self._rpc
            if rpc is None:
                from storage.supabase_client import rpc
            return await rpc(fn, payload)
        except RuntimeError as exc:
            raise StoreUnavailable(f"{fn}: {exc}") from exc

    async def _dict(self, fn: str, payload: dict) -> dict:
        out = await self._call(fn, payload)
        if isinstance(out, str):
            try:
                out = json.loads(out)
            except ValueError:
                pass
        if not isinstance(out, dict):
            raise StoreUnavailable(f"{fn}: unexpected answer shape")
        return out

    async def ready(self) -> bool:
        try:
            out = await self._dict("oauth_ready", {})
        except StoreUnavailable:
            return False
        return out.get("ready") is True

    async def client_register(self, client_id: str, name: str, redirect_uris: list, ip_hash: str) -> bool:
        out = await self._dict("oauth_client_register", {
            "p_client_id": client_id, "p_client_name": name,
            "p_redirect_uris": redirect_uris, "p_ip_hash": ip_hash})
        return out.get("stored") is True

    async def client_get(self, client_id: str) -> Optional[dict]:
        out = await self._dict("oauth_client_get", {"p_client_id": client_id})
        return out if out.get("found") else None

    async def request_create(self, request_id: str, client_id: str, redirect_uri: str, code_challenge: str,
                             scope: str, state: Optional[str], resource: str, ttl_s: int) -> None:
        out = await self._dict("oauth_request_create", {
            "p_request_id": request_id, "p_client_id": client_id, "p_redirect_uri": redirect_uri,
            "p_code_challenge": code_challenge, "p_scope": scope, "p_state": state or "",
            "p_resource": resource, "p_ttl_seconds": ttl_s})
        if out.get("created") is not True:
            raise StoreUnavailable("oauth_request_create: not created")

    async def request_get(self, request_id: str) -> Optional[dict]:
        out = await self._dict("oauth_request_get", {"p_request_id": request_id})
        return out if out.get("found") else None

    async def request_set_email(self, request_id: str, poll_hash: str, magic_hash: str, email_digest: str,
                                email_hint: str, min_gap_s: int, max_sends: int) -> dict:
        return await self._dict("oauth_request_set_email", {
            "p_request_id": request_id, "p_poll_hash": poll_hash, "p_magic_hash": magic_hash,
            "p_email_hash": email_digest, "p_email_hint": email_hint,
            "p_min_gap_seconds": min_gap_s, "p_max_sends": max_sends})

    async def request_lookup_magic(self, magic_hash: str) -> Optional[dict]:
        out = await self._dict("oauth_request_lookup_magic", {"p_magic_hash": magic_hash})
        return out if out.get("found") else None

    async def request_decide(self, magic_hash: str, approve: bool) -> dict:
        return await self._dict("oauth_request_decide", {"p_magic_hash": magic_hash, "p_approve": approve})

    async def request_poll(self, request_id: str, poll_hash: str) -> str:
        out = await self._dict("oauth_request_poll", {"p_request_id": request_id, "p_poll_hash": poll_hash})
        return str(out.get("status") or "unknown")

    async def request_complete(self, request_id: str, poll_hash: str, code_hash: str, code_ttl_s: int) -> dict:
        return await self._dict("oauth_request_complete", {
            "p_request_id": request_id, "p_poll_hash": poll_hash, "p_code_hash": code_hash,
            "p_code_ttl_seconds": code_ttl_s})

    async def code_consume(self, code_hash: str) -> dict:
        return await self._dict("oauth_code_consume", {"p_code_hash": code_hash})

    async def refresh_store(self, token_hash: str, family_id: str, client_id: str, email_digest: str,
                            scope: str, resource: str, ttl_s: int, family_ttl_s: int) -> None:
        out = await self._dict("oauth_refresh_store", {
            "p_token_hash": token_hash, "p_family_id": family_id, "p_client_id": client_id,
            "p_email_hash": email_digest, "p_scope": scope, "p_resource": resource,
            "p_ttl_seconds": ttl_s, "p_family_ttl_seconds": family_ttl_s})
        if out.get("stored") is not True:
            raise StoreUnavailable("oauth_refresh_store: not stored")

    async def refresh_rotate(self, old_hash: str, new_hash: str, client_id: str, ttl_s: int) -> dict:
        return await self._dict("oauth_refresh_rotate", {
            "p_old_hash": old_hash, "p_new_hash": new_hash, "p_client_id": client_id, "p_ttl_seconds": ttl_s})

    async def refresh_revoke(self, token_hash: str, client_id: str) -> bool:
        out = await self._dict("oauth_refresh_revoke", {"p_token_hash": token_hash, "p_client_id": client_id})
        return out.get("revoked") is True

    async def account_for_email(self, email_digest: str) -> Optional[dict]:
        out = await self._dict("oauth_account_for_email", {"p_email_hash": email_digest})
        return out if out.get("found") else None

    async def account_link(self, email_digest: str, account_id: str, customer_id: Optional[str],
                           plan: Optional[str]) -> bool:
        out = await self._dict("oauth_account_link", {
            "p_email_hash": email_digest, "p_account_id": account_id,
            "p_customer_id": customer_id, "p_plan": plan})
        return out.get("linked") is True


# ---------------------------------------------------------------------------
# The in-memory twin
# ---------------------------------------------------------------------------

class MemoryStore:
    """Same contract, same answers, same state machine as the SQL - see the module docstring."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self.now = clock
        self.clients: dict = {}
        self.requests: dict = {}
        self.magic: dict = {}
        self.codes: dict = {}
        self.refresh: dict = {}
        self.links: dict = {}
        self.accounts: dict = {}          # test hook: email digest -> credit-account row (pre-link accounts)

    async def ready(self) -> bool:
        return True

    # clients
    async def client_register(self, client_id, name, redirect_uris, ip_hash):
        if not (isinstance(redirect_uris, list) and 1 <= len(redirect_uris) <= 10):
            raise StoreUnavailable("bad redirect_uris")
        self.clients.setdefault(client_id, {"client_name": (name or "")[:120], "redirect_uris": list(redirect_uris)})
        return True

    async def client_get(self, client_id):
        c = self.clients.get(client_id)
        return {"found": True, **c} if c else None

    # sign-ins
    def _status(self, r):
        return "expired" if r["expires_at"] < self.now() and r["status"] != "completed" else r["status"]

    async def request_create(self, request_id, client_id, redirect_uri, code_challenge, scope, state,
                             resource, ttl_s):
        self.requests[request_id] = {
            "request_id": request_id, "client_id": client_id, "redirect_uri": redirect_uri,
            "code_challenge": code_challenge, "scope": scope, "state": state or None, "resource": resource,
            "poll_hash": None, "magic_hash": None, "email_hash": None, "email_hint": None,
            "status": "new", "email_sent_count": 0, "last_email_at": None,
            "expires_at": self.now() + max(60, min(ttl_s, 1800)),
        }

    async def request_get(self, request_id):
        r = self.requests.get(request_id)
        if not r:
            return None
        return {"found": True, "client_id": r["client_id"], "redirect_uri": r["redirect_uri"],
                "scope": r["scope"], "state": r["state"], "resource": r["resource"],
                "email_hint": r["email_hint"], "status": self._status(r),
                "email_sent_count": r["email_sent_count"]}

    async def request_set_email(self, request_id, poll_hash, magic_hash, email_digest, email_hint,
                                min_gap_s, max_sends):
        r = self.requests.get(request_id)
        if not r:
            return {"ok": False, "reason": "not_found"}
        if r["expires_at"] < self.now():
            return {"ok": False, "reason": "expired"}
        if r["status"] not in ("new", "email_sent"):
            return {"ok": False, "reason": "bad_state"}
        if r["poll_hash"] is not None and r["poll_hash"] != poll_hash:
            return {"ok": False, "reason": "poll_mismatch"}
        if r["email_sent_count"] >= max(1, min(max_sends, 10)):
            return {"ok": False, "reason": "too_many"}
        if r["last_email_at"] is not None and self.now() - r["last_email_at"] < max(0, min(min_gap_s, 300)):
            return {"ok": False, "reason": "too_soon"}
        if r["magic_hash"]:
            self.magic.pop(r["magic_hash"], None)
        r.update(poll_hash=poll_hash, magic_hash=magic_hash, email_hash=email_digest,
                 email_hint=email_hint[:120], status="email_sent", last_email_at=self.now())
        r["email_sent_count"] += 1
        self.magic[magic_hash] = request_id
        return {"ok": True, "sends": r["email_sent_count"]}

    async def request_lookup_magic(self, magic_hash):
        rid = self.magic.get(magic_hash)
        r = self.requests.get(rid) if rid else None
        if not r:
            return None
        return {"found": True, "request_id": rid, "client_id": r["client_id"],
                "redirect_uri": r["redirect_uri"], "scope": r["scope"], "resource": r["resource"],
                "email_hint": r["email_hint"], "status": self._status(r)}

    async def request_decide(self, magic_hash, approve):
        rid = self.magic.get(magic_hash)
        r = self.requests.get(rid) if rid else None
        if not r:
            return {"ok": False, "reason": "not_found"}
        if r["status"] == "email_sent" and r["expires_at"] > self.now():
            r["status"] = "verified" if approve else "denied"
            return {"ok": True, "request_id": rid, "status": r["status"]}
        if r["expires_at"] <= self.now() and r["status"] != "completed":
            return {"ok": False, "reason": "expired"}
        return {"ok": False, "reason": "already_used", "request_id": rid, "status": r["status"]}

    async def request_poll(self, request_id, poll_hash):
        r = self.requests.get(request_id)
        if not r or r["poll_hash"] is None or r["poll_hash"] != poll_hash:
            return "unknown"
        return self._status(r)

    async def request_complete(self, request_id, poll_hash, code_hash, code_ttl_s):
        r = self.requests.get(request_id)
        if not r or r["poll_hash"] is None or r["poll_hash"] != poll_hash:
            return {"ok": False, "reason": "unknown"}
        if r["expires_at"] < self.now() and r["status"] != "completed":
            return {"ok": False, "reason": "expired"}
        if r["status"] == "completed":
            return {"ok": False, "reason": "completed"}
        if r["status"] not in ("verified", "denied"):
            return {"ok": False, "reason": "not_ready"}
        if r["status"] == "denied":
            r["status"] = "completed"
            return {"ok": True, "outcome": "denied", "client_id": r["client_id"],
                    "redirect_uri": r["redirect_uri"], "state": r["state"]}
        if r["email_hash"] is None:
            return {"ok": False, "reason": "not_ready"}
        self.codes[code_hash] = {
            "request_id": request_id, "client_id": r["client_id"], "redirect_uri": r["redirect_uri"],
            "code_challenge": r["code_challenge"], "scope": r["scope"], "resource": r["resource"],
            "email_hash": r["email_hash"], "expires_at": self.now() + max(30, min(code_ttl_s, 600)),
            "used_at": None}
        r["status"] = "completed"
        return {"ok": True, "outcome": "approved", "client_id": r["client_id"],
                "redirect_uri": r["redirect_uri"], "state": r["state"]}

    # codes / refresh
    async def code_consume(self, code_hash):
        c = self.codes.get(code_hash)
        if c and c["used_at"] is None and c["expires_at"] > self.now():
            c["used_at"] = self.now()
            return {"ok": True, **{k: c[k] for k in (
                "request_id", "client_id", "redirect_uri", "code_challenge", "scope", "resource", "email_hash")}}
        if c and c["used_at"] is not None:
            for t in self.refresh.values():
                if t["family_id"] == c["request_id"] and t["revoked_at"] is None:
                    t["revoked_at"] = self.now()
            return {"ok": False, "reason": "reused"}
        return {"ok": False, "reason": "invalid"}

    async def refresh_store(self, token_hash, family_id, client_id, email_digest, scope, resource,
                            ttl_s, family_ttl_s):
        self.refresh.setdefault(token_hash, {
            "family_id": family_id, "client_id": client_id, "email_hash": email_digest, "scope": scope,
            "resource": resource, "expires_at": self.now() + max(60, min(ttl_s, 7776000)),
            "family_expires_at": self.now() + max(60, min(family_ttl_s, 15552000)),
            "rotated_at": None, "revoked_at": None})

    async def refresh_rotate(self, old_hash, new_hash, client_id, ttl_s):
        t = self.refresh.get(old_hash)
        if not t:
            return {"ok": False, "reason": "invalid"}
        if t["client_id"] != client_id:
            return {"ok": False, "reason": "client_mismatch"}
        if t["revoked_at"] is not None:
            return {"ok": False, "reason": "invalid"}
        if t["rotated_at"] is not None:
            for o in self.refresh.values():
                if o["family_id"] == t["family_id"] and o["revoked_at"] is None:
                    o["revoked_at"] = self.now()
            return {"ok": False, "reason": "reuse"}
        if t["expires_at"] < self.now() or t["family_expires_at"] < self.now():
            return {"ok": False, "reason": "expired"}
        t["rotated_at"] = self.now()
        self.refresh[new_hash] = {
            **{k: t[k] for k in ("family_id", "client_id", "email_hash", "scope", "resource", "family_expires_at")},
            "expires_at": min(self.now() + max(60, min(ttl_s, 7776000)), t["family_expires_at"]),
            "rotated_at": None, "revoked_at": None}
        return {"ok": True, "family_id": t["family_id"], "email_hash": t["email_hash"],
                "scope": t["scope"], "resource": t["resource"]}

    async def refresh_revoke(self, token_hash, client_id):
        t = self.refresh.get(token_hash)
        if not t or t["client_id"] != client_id:
            return False
        for o in self.refresh.values():
            if o["family_id"] == t["family_id"] and o["revoked_at"] is None:
                o["revoked_at"] = self.now()
        return True

    # accounts
    async def account_for_email(self, email_digest):
        if email_digest in self.links:
            return {"found": True, **self.links[email_digest]}
        row = self.accounts.get(email_digest)
        if row and str(row.get("account_id", "")).startswith("sub_"):
            return {"found": True, **row}
        return None

    async def account_link(self, email_digest, account_id, customer_id, plan):
        if email_digest in self.links:
            return False
        if not account_id.startswith("sub_"):
            raise StoreUnavailable("bad account id")
        self.links[email_digest] = {"account_id": account_id, "customer_id": customer_id, "plan": plan}
        return True


# ---------------------------------------------------------------------------
# Which store, and is it ready
# ---------------------------------------------------------------------------

_STORE: Optional[object] = None


def get_store():
    return _STORE if _STORE is not None else SpineStore()


def set_store(store: Optional[object]) -> None:
    """Install a store (tests, local development); None restores the spine. Also forgets the readiness verdict."""
    global _STORE
    _STORE = store
    reset_readiness()


_READY = {"ok": False, "until": 0.0}


def reset_readiness() -> None:
    _READY.update(ok=False, until=0.0)


async def ready() -> bool:
    """Can the sign-in work right now? Cached (60 s when yes, 10 s when no) so this can sit in the path of
    `tools/list` and of every refused call. A database that is merely slow counts as not ready: advertising a
    sign-in that cannot complete is worse than advertising none."""
    now = time.monotonic()
    if now < _READY["until"]:
        return bool(_READY["ok"])
    try:
        import asyncio
        ok = bool(await asyncio.wait_for(get_store().ready(), timeout=2.5))
    except Exception:  # noqa: BLE001
        ok = False
    _READY.update(ok=ok, until=now + (60.0 if ok else 10.0))
    return ok
