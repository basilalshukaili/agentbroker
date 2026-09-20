"""Idempotency gate for tools/call — delivers the ADVERTISED retry contract
(README/well_known: dedupe on (agent_id, operation, idempotency_key), safe to
retry) that was documented but never wired into dispatch.

Semantics (Stripe-style, success-only):
  - A write tool called with an `idempotency_key` (argument or
    X-Idempotency-Key header) is deduped per (agent scope, tool, key).
  - REPLAY with the same key + same arguments  -> the ORIGINAL response is
    returned verbatim: no re-execution, no second side effect, no new charge.
    This protects the cardinal no-double-charge rule under the real failure
    mode: agent times out -> blindly retries.
  - Same key + DIFFERENT arguments -> `idempotency_conflict` (per api/errors.md).
  - Only SUCCESSFUL responses are stored. Failures are never pinned, so a
    retry after a transient error (billing_unavailable, upstream down) runs
    again — correct, and safe because failures charge nothing.
  - TWO CONCURRENT CALLS with the same key -> only the FIRST to arrive
    executes the real tool; the second gets an honest "already in flight,
    retry with the same key" refusal instead of running the tool a second
    time. Same for a retry that lands after this process crashed mid-flight
    (killed after claiming the key, before completing it): the NEXT claim
    (in this process after a restart, or a peer process) sees the durable
    PENDING marker written before dispatch and refuses to execute again
    rather than guessing that "no in-memory record" means "safe to run".
    See claim()'s own docstring — this is the fix for the hole in the
    original get()-then-put() pair, which only ever wrote durably AFTER a
    successful execution and therefore protected neither case.

Storage: in-memory (storage.idempotency_store, 24h TTL, atomic claim) +
best-effort durable mirror in Supabase table `idempotency_keys` so the
guarantee survives restarts. Supabase calls are hard-timeout-bounded and
FAIL OPEN to the in-memory verdict — the gate must never block or slow tool
dispatch (same lesson as the quota gate), and same-process concurrency is
fully protected by the in-memory claim alone even when Supabase is down or
unconfigured.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Any, Optional

from storage.idempotency_store import get_idempotency_store

logger = logging.getLogger("smb_broker.idempotency")

_SB_TIMEOUT_S = 2.0
_TABLE = "idempotency_keys"


def args_hash(arguments: dict) -> str:
    """Canonical hash of the tool arguments (idempotency_key already popped)."""
    try:
        blob = json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)
    except Exception:  # noqa: BLE001
        blob = repr(arguments)
    return hashlib.sha256(blob.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Durable-layer helpers — each does its OWN deferred import of
# storage.supabase_client, exactly like the original get()/put() did, so
# patching storage.supabase_client.<fn> at the definition site still reaches
# these (see tests/unit/test_a_stubbed_gate_is_actually_stubbed.py's fragile-
# import scan). Split out from claim()/complete()/release() so a test that
# wants to fake the durable layer directly (e.g. to simulate a separate
# process's empty in-memory store against a shared fake table) can patch
# these three names instead of stubbing httpx.
# ---------------------------------------------------------------------------

async def _durable_lookup(scope: str, tool: str, key: str) -> Optional[dict[str, Any]]:
    """The raw durable row for this key, or None (missing, unconfigured, or
    any failure — bounded and fail-open, never raises)."""
    try:
        from storage.supabase_client import select_rows
        rows = await asyncio.wait_for(
            select_rows(_TABLE, filters={
                "agent_scope": scope, "operation": tool, "idem_key": key,
            }, limit=1),
            timeout=_SB_TIMEOUT_S,
        )
        return rows[0] if rows else None
    except Exception as exc:  # noqa: BLE001 - includes TimeoutError
        logger.debug("idem_durable_lookup_failed scope=%s tool=%s err=%s", scope, tool, exc)
        return None


async def _durable_claim(scope: str, tool: str, key: str) -> None:
    """Best-effort durable PENDING marker, written BEFORE the tool executes
    so a process that crashes mid-flight leaves a trace the NEXT claim()
    (from this process after a restart, or a peer) will see. Best-effort:
    if this insert is lost (Supabase down), same-process concurrency is
    still fully protected by the in-memory reserve(); only cross-process /
    post-restart protection degrades to "not configured", not to a false
    guarantee."""
    try:
        from storage.supabase_client import insert_row
        await asyncio.wait_for(
            insert_row(_TABLE, {
                "agent_scope": scope, "operation": tool, "idem_key": key,
                "status": "pending",
            }),
            timeout=_SB_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("idem_durable_claim_failed scope=%s tool=%s err=%s", scope, tool, exc)


async def _durable_resolve(
    scope: str, tool: str, key: str, status: str,
    ahash: Optional[str] = None, response: Optional[dict] = None,
) -> None:
    """Best-effort durable UPDATE of the pending marker to its final state
    ("complete" with the replayable response, or "released" after a
    transient failure so a legitimate retry is not durably blocked
    forever)."""
    patch: dict[str, Any] = {"status": status}
    if ahash is not None:
        patch["args_hash"] = ahash
    if response is not None:
        patch["response"] = response
    try:
        from storage.supabase_client import update_row
        await asyncio.wait_for(
            update_row(_TABLE, {
                "agent_scope": scope, "operation": tool, "idem_key": key,
            }, patch),
            timeout=_SB_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("idem_durable_resolve_failed scope=%s tool=%s status=%s err=%s",
                     scope, tool, status, exc)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def claim(scope: str, tool: str, key: str) -> tuple[str, Optional[dict[str, Any]]]:
    """Attempt to claim (scope, tool, key) for execution.

    Returns:
      ("claimed", None)      — the caller now OWNS this key: execute the
                                tool exactly once, then call complete() on
                                success or release() on failure.
      ("in_progress", None)  — someone already holds this key and has not
                                finished: this process (a genuinely
                                concurrent duplicate call), or a process that
                                crashed after claiming but before completing.
                                Those two cases are indistinguishable from
                                here, and guessing "it crashed, safe to run"
                                is exactly the double-booking/double-charge
                                hazard this gate exists to close, so both are
                                refused the same way. The caller MUST NOT
                                execute the tool.
      ("complete", outcome)  — a prior call already finished; replay
                                `outcome` (`{"args_hash":..., "response":...}`)
                                verbatim.
    """
    status, outcome = get_idempotency_store().reserve(scope, tool, key)
    if status != "claimed":
        return status, outcome

    # The in-memory store said "claimed" — but a FRESH PROCESS (after a
    # restart) has an empty in-memory store no matter how many times this
    # exact key has already been claimed elsewhere, so that verdict alone
    # does not prove no one else is mid-flight. Check the durable record
    # before trusting it.
    row = await _durable_lookup(scope, tool, key)
    if row is not None:
        row_status = row.get("status")
        if row_status == "complete":
            rec = {"args_hash": row.get("args_hash", ""), "response": row.get("response") or {}}
            get_idempotency_store().complete(scope, tool, key, rec)
            return "complete", rec
        if row_status == "pending":
            # A durable PENDING row already exists: either a concurrent
            # process holds it right now, or a prior process crashed before
            # marking it complete. Give up our own (wrong) optimistic
            # in-memory claim and refuse to execute.
            get_idempotency_store().release(scope, tool, key)
            return "in_progress", None
        # Any other status ("released" from a prior transient failure, or an
        # unrecognised value) does not block a fresh claim — fall through
        # and durably reclaim it below.

    await _durable_claim(scope, tool, key)
    return "claimed", None


async def complete(scope: str, tool: str, key: str, ahash: str, response: dict) -> None:
    """Resolve a held claim as a SUCCESS whose response future replays return
    verbatim. Best-effort on the durable side; never raises."""
    rec = {"args_hash": ahash, "response": response}
    get_idempotency_store().complete(scope, tool, key, rec)
    await _durable_resolve(scope, tool, key, "complete", ahash=ahash, response=response)


async def release(scope: str, tool: str, key: str) -> None:
    """Drop a held claim after a failed/transient execution so a legitimate
    retry with the SAME key can proceed — mirrors the original "only
    successful responses are stored" rule, extended to the pending marker
    itself. Best-effort on the durable side; never raises."""
    get_idempotency_store().release(scope, tool, key)
    await _durable_resolve(scope, tool, key, "released")
