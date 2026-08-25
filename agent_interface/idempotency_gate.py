"""
Idempotency gate for tools/call — delivers the ADVERTISED retry contract
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

Storage: in-memory (storage.idempotency_store, 24h TTL) + best-effort durable
mirror in Supabase table `idempotency_keys` so the guarantee survives restarts.
Supabase calls are hard-timeout-bounded and FAIL OPEN to memory-only — the
gate must never block or slow tool dispatch (same lesson as the quota gate).
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


async def get(scope: str, tool: str, key: str) -> Optional[dict[str, Any]]:
    """Return {'args_hash':..., 'response':...} if this key was seen, else None."""
    rec = get_idempotency_store().get(scope, tool, key)
    if rec is not None:
        return rec
    # Durable fallback (survives restarts) — bounded, fail-open.
    try:
        from storage.supabase_client import select_rows
        rows = await asyncio.wait_for(
            select_rows(_TABLE, filters={
                "agent_scope": scope, "operation": tool, "idem_key": key,
            }, limit=1),
            timeout=_SB_TIMEOUT_S,
        )
        if rows:
            row = rows[0]
            rec = {
                "args_hash": row.get("args_hash", ""),
                "response": row.get("response") or {},
            }
            # Rehydrate memory so subsequent replays are instant.
            get_idempotency_store().set(scope, tool, key, rec)
            return rec
    except Exception as exc:  # noqa: BLE001 - includes TimeoutError
        logger.debug("idem_get_durable_miss scope=%s tool=%s err=%s", scope, tool, exc)
    return None


async def put(scope: str, tool: str, key: str, ahash: str, response: dict) -> None:
    """Record a SUCCESSFUL response for replay. Best-effort, never raises."""
    rec = {"args_hash": ahash, "response": response}
    try:
        get_idempotency_store().set(scope, tool, key, rec)
    except Exception:  # noqa: BLE001
        pass
    try:
        from storage.supabase_client import insert_row
        await asyncio.wait_for(
            insert_row(_TABLE, {
                "agent_scope": scope, "operation": tool, "idem_key": key,
                "args_hash": ahash, "response": response,
            }),
            timeout=_SB_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("idem_put_durable_skip scope=%s tool=%s err=%s", scope, tool, exc)
