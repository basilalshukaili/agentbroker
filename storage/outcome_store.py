"""
Outcome store — persists terminal OutcomeReceipts and async job states.

FIX 1 (2026-08-23): Added durable Supabase backend so get_status and
get_outcome return real records across requests.  The in-memory dict is kept
as a write-through cache and test backend.

Write path  : every set_complete() call writes in-memory AND fires a
              best-effort Supabase upsert (fail-open — a store failure never
              breaks the tool call).

Read path   : get_async() checks in-memory first (O(1), handles same-request
              and test cases), then falls back to a Supabase SELECT by PK.
              get() stays synchronous for backward-compat / unit tests.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("smb_broker.outcome_store")


class OutcomeStoreUnavailable(RuntimeError):
    """The durable store could not be reached or queried for this read.

    FIX (2026-09-21): measured on the live service -- the production
    container had NO Supabase configuration at all (SUPABASE_URL,
    SUPABASE_SERVICE_KEY, SUPABASE_SERVICE_ROLE_KEY, SUPABASE_ANON_KEY all
    absent), and get_async()'s Supabase fallback used to call the lenient
    `select_rows`, which is contractually incapable of raising and returns
    [] on a missing config exactly as it does on a real empty result (see
    storage/supabase_client.py's own docstring on why the *_strict variants
    exist). get_status/get_outcome then reported a completed, existing
    operation as "not_found" -- a false statement of fact about the
    service's own work.

    This exception is the distinguishing signal: raised by get_async() ONLY
    when the durable store itself could not be reached or queried. A
    genuine miss -- the store was reached and confirmed no such row --
    still returns None from get_async(), exactly as before. Callers
    (core/status_outcome.py's handle_get_status / handle_get_outcome) must
    turn this into a retryable service error, never into not_found.

    TRANSPORT UPDATE (board row 206, 2026-09-22): _supabase_fetch no longer
    calls select_rows_strict against the raw table -- this service now
    deploys with ONLY the Supabase anon key (no service-role key on a public
    box; see sql/agentbroker/001_operations_security_definer_rpc.sql), and
    the anon role has no grant on `operations` at all. Reads/writes go
    through narrow SECURITY DEFINER RPCs instead. This exception's contract
    (raised iff the store could not be reached/queried, None iff it was
    reached and confirmed empty) is unchanged by that -- only the wire call
    underneath it moved.
    """


class OutcomeStore:
    def __init__(self) -> None:
        self._records: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Write helpers
    # ------------------------------------------------------------------

    def set_pending(
        self,
        operation_id: str,
        operation_type: str,
        *,
        agent_id: Optional[str] = None,
    ) -> None:
        from core.ownership import owner_for_storage

        # THE OWNER MUST BE RECORDED AT CREATION, NOT ONLY AT COMPLETION.
        #
        # A booking is async by default: this is the FIRST write for an
        # operation, minted before the Celery task even runs, and
        # get_status/get_outcome can be polled against it immediately. A
        # caller that never got charged an owner here was unowned for the
        # entire "pending" window - which is exactly the gap between
        # "authenticated at the door" and "attributed in storage" this fix
        # closes for every producer, not only the terminal one.
        owner = owner_for_storage(agent_id)
        record: dict[str, Any] = {
            "operation_id": operation_id,
            "operation_type": operation_type,
            "status": "pending",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if owner:
            record["agent_id"] = owner
        self._records[operation_id] = record
        self._fire_persist(operation_id, record, tool=operation_type, agent_id=owner)

    def set_executing(self, operation_id: str) -> None:
        if operation_id in self._records:
            self._records[operation_id]["status"] = "executing"
            self._records[operation_id]["updated_at"] = datetime.now(timezone.utc).isoformat()
            self._fire_persist(operation_id, self._records[operation_id])

    def set_complete(
        self,
        operation_id: str,
        outcome: dict[str, Any],
        *,
        tool: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> None:
        from core.ownership import owner_for_storage

        if operation_id not in self._records:
            self._records[operation_id] = {"operation_id": operation_id}
        self._records[operation_id].update({
            "status": outcome.get("status", "success"),
            "outcome": outcome,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        # KEEP THE OWNER WHERE THE READ PATH LOOKS.
        #
        # agent_id was passed straight to the Supabase upsert and never kept
        # in memory, and get_async answers from memory first - so the owner of
        # an operation was unreadable on the very path that has to check it,
        # and get_status/get_outcome returned other agents' receipts to
        # whoever asked.
        #
        # Only ever SET it, never clear it: send_message writes this row once
        # from inside the handler and the MCP dispatcher writes it again after
        # the fact, and a later write that happened to carry no identity must
        # not turn an owned record into an unowned (world-readable) one.
        owner = owner_for_storage(agent_id)
        if owner:
            self._records[operation_id]["agent_id"] = owner
        # Persist the owner we HOLD, not the one this call happened to carry:
        # the upsert overwrites the column, so passing None on the second
        # write would clear the owner durably as well as in memory.
        self._fire_persist(operation_id, self._records[operation_id], outcome=outcome,
                           tool=tool,
                           agent_id=self._records[operation_id].get("agent_id"))

    async def set_complete_durable(
        self,
        operation_id: str,
        outcome: dict[str, Any],
        *,
        tool: Optional[str] = None,
        agent_id: Optional[str] = None,
        timeout: float = 3.0,
    ) -> bool:
        """Like set_complete, but AWAITS the durable write and reports
        whether it actually landed, instead of firing it and hoping.

        set_complete's own durable write is fire-and-forget
        (asyncio.ensure_future, never awaited) precisely so a slow or
        unreachable Supabase never adds latency to the common case -- right
        for most operations, but wrong for the few whose loss matters: a
        CONFIRMED, CHARGED booking, a cancellation that actually ran, a
        booking pending the provider's own confirmation, or an outcome whose
        fate is UNKNOWN. For those, a fire-and-forget write can lose the
        race against this very process being killed a moment later (a
        redeploy, an OOM, a crash) with NOTHING to show it ever ran -- a
        different process (a horizontally scaled peer, this same process
        after an automatic restart, or the idempotency gate deciding whether
        a retry is safe) then sees "operation not found" for a real
        appointment. core/schedule_appointment.py calls this only for those
        outcomes; every other caller keeps the cheap fire-and-forget path.

        Returns True once the durable write is CONFIRMED to have landed,
        False on any failure or timeout. Still fails open for the caller's
        own response -- the receipt already reflects a known, honest
        outcome regardless of whether this durability layer succeeds; only
        a second process's ability to independently discover that outcome
        depends on this returning True.
        """
        self.set_complete(operation_id, outcome, tool=tool, agent_id=agent_id)
        record = self._records.get(operation_id, {})
        try:
            return bool(await asyncio.wait_for(
                _supabase_upsert(
                    operation_id, record, outcome, tool, record.get("agent_id")),
                timeout=timeout,
            ))
        except Exception as exc:  # noqa: BLE001 - includes asyncio.TimeoutError
            logger.warning(
                "outcome_store_durable_persist_failed id=%s err=%s",
                operation_id, exc)
            return False

    # ------------------------------------------------------------------
    # Read helpers
    # ------------------------------------------------------------------

    def get(self, operation_id: str) -> Optional[dict[str, Any]]:
        """Synchronous get — in-memory only.  Used by tests and legacy callers."""
        return self._records.get(operation_id)

    async def get_async(self, operation_id: str) -> Optional[dict[str, Any]]:
        """
        Async get — checks in-memory first, then Supabase.

        Returns None ONLY for a genuine miss: not in memory, AND the
        durable store was reached and confirmed no such row. Raises
        OutcomeStoreUnavailable when the durable store could not be reached
        or queried at all -- this is deliberately NOT swallowed into None
        here. Doing so was the bug: it collapsed "I could not check" and
        "I checked and there is nothing" into the same answer, and
        core/status_outcome.py reported both as not_found. Let it propagate;
        the two handlers there are what must turn it into a retryable
        service error.
        """
        local = self._records.get(operation_id)
        if local:
            return local
        # Fall back to Supabase (durable, cross-request). _supabase_fetch
        # raises OutcomeStoreUnavailable rather than returning None when it
        # could not even ask -- do not catch it here.
        row = await _supabase_fetch(operation_id)
        if row:
            # result_json is stored (and comes back from PostgREST) as a
            # TEXT column holding a JSON STRING (see _supabase_upsert:
            # `_json.dumps(persisted_outcome, ...)`), not a parsed object.
            # The in-memory path's "outcome" is always the original dict
            # handed to set_complete(); this branch used to hand back the
            # raw, un-parsed STRING under the same key, so every consumer
            # (core/status_outcome.py's `OutcomeReceipt(**outcome)` and
            # its own `outcome.get(...)` fallback) crashed with a
            # TypeError/AttributeError instead of returning a receipt --
            # on EVERY cross-process get_status/get_outcome call for a
            # completed operation, the one case this fallback branch
            # exists to serve. Parse it back into the same shape the
            # in-memory path already returns.
            raw_outcome = row.get("result_json")
            if isinstance(raw_outcome, str):
                try:
                    parsed_outcome = json.loads(raw_outcome) if raw_outcome else {}
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "outcome_store_result_json_unparseable id=%s",
                        operation_id)
                    parsed_outcome = {}
            elif isinstance(raw_outcome, dict):
                parsed_outcome = raw_outcome
            else:
                parsed_outcome = {}
            # Reconstruct the envelope the handlers expect
            return {
                "operation_id": row.get("operation_id", operation_id),
                "status": row.get("status", "unknown"),
                "outcome": parsed_outcome,
                "updated_at": row.get("ts"),
                "partial_result": None,
                # The durable row has carried an owner since the operations
                # table was migrated, and this envelope dropped it - so a
                # cross-request read (the only kind this branch serves) had
                # nothing to check ownership against.
                "agent_id": row.get("agent_id"),
            }
        return None

    def exists(self, operation_id: str) -> bool:
        return operation_id in self._records

    async def get_appointment_owner_async(self, appointment_id: str) -> Optional[str]:
        """The agent_id that owns a CONFIRMED appointment, keyed by the
        PROVIDER's own booking id -- not our operation_id.

        A caller asking to cancel a booking only ever holds the provider id
        (Cal.com's uid); it never learns the operation_id that created it.
        So the cancellation-authorization check in
        core/schedule_appointment.py cannot reuse the operation_id lookup
        get_async already does -- it has to search by the OTHER identifier.

        Checks in-memory first (covers the same-process case every unit
        test exercises), then falls back to the durable table, mirroring
        the get_async() pattern above. Returns None when no CONFIRMED
        booking anywhere carries this appointment_id -- the caller
        (schedule_appointment.py) then treats that exactly like an unowned
        operation receipt: denied to everyone, not granted to whoever asks
        first. That is the same fail-closed policy core/ownership.py's
        read_denial already enforces for reads; this function only supplies
        the OWNER it needs, it does not re-decide the policy.
        """
        if not appointment_id:
            return None
        for rec in self._records.values():
            outcome = rec.get("outcome") or {}
            result = outcome.get("result") or {}
            if (result.get("appointment_id") == appointment_id
                    and outcome.get("reason_code") == "appointment_confirmed"):
                return rec.get("agent_id")
        try:
            row = await _supabase_fetch_by_appointment_id(appointment_id)
            if row:
                return row.get("agent_id")
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "outcome_store_appointment_lookup_failed id=%s err=%s",
                appointment_id, exc)
        return None

    # ------------------------------------------------------------------
    # Durable write (fire-and-forget)
    # ------------------------------------------------------------------

    def _fire_persist(
        self,
        operation_id: str,
        record: dict,
        outcome: Optional[dict] = None,
        tool: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> None:
        """Schedule a best-effort Supabase upsert.  Never raises."""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(
                    _supabase_upsert(operation_id, record, outcome, tool, agent_id)
                )
        except Exception:  # noqa: BLE001
            pass


async def _supabase_upsert(
    operation_id: str,
    record: dict,
    outcome: Optional[dict],
    tool: Optional[str],
    agent_id: Optional[str],
) -> bool:
    """Write one operation row to Supabase.  Never raises — fail-open.

    Returns True once the row is CONFIRMED written, False otherwise. The
    fire-and-forget caller (_fire_persist) ignores this; set_complete_durable
    awaits it precisely so it can tell the difference.

    Routed through the operations_upsert RPC (board row 206 item 1): the anon
    key this container deploys with has no direct INSERT/UPDATE grant on
    `operations`, so a raw upsert_row("operations", ...) call would now fail
    every time. operations_upsert is SECURITY DEFINER, granted to anon, and
    can only ever write the single row named by its own parameters (an
    operation_id + its receipt fields) -- never an arbitrary table/row.
    """
    try:
        import json as _json
        from storage.supabase_client import rpc
        # FIX (quota strip, belt-and-suspenders): strip the transient per-call
        # `quota` block from the serialised result even if the caller somehow
        # passed a mutated dict.  Quota belongs to the response envelope only.
        _EPHEMERAL_KEYS = frozenset({"quota"})
        persisted_outcome = (
            {k: v for k, v in outcome.items() if k not in _EPHEMERAL_KEYS}
            if outcome else None
        )
        payload = {
            "p_operation_id": operation_id,
            "p_tool": tool or record.get("operation_type") or "unknown",
            "p_status": record.get("status", "unknown"),
            "p_reason_code": (outcome or {}).get("reason_code"),
            # THE OTHER KEY A CANCELLATION IS LOOKED UP BY. A cancel caller
            # holds the provider's own booking id, never our operation_id --
            # so get_appointment_owner_async (above) has to find this row by
            # THIS column, not by operation_id. Only ever populated for a
            # confirmed booking (result.appointment_id is only ever set on
            # that outcome shape); every other row keeps this NULL.
            "p_appointment_id": ((outcome or {}).get("result") or {}).get("appointment_id"),
            "p_result_json": (
                _json.dumps(persisted_outcome, default=str) if persisted_outcome else None
            ),
            "p_agent_id": agent_id,
        }
        # CHECK THE RESULT. The old upsert_row returned None on failure and
        # could not raise, so the handler below was dead code - and a lost
        # write means a later get_status/get_outcome answers "unknown
        # operation", which a caller reads as "that never happened" rather
        # than "we lost the record of it". rpc() DOES raise on failure (see
        # below), but this write path stays fail-open by design (module
        # docstring: "a store failure never breaks the tool call") -- so the
        # exception is still caught here, just never silently discarded
        # without a warning log.
        written = await rpc("operations_upsert", payload)
        if not written:
            logger.warning(
                "outcome_store_persist_failed id=%s -- the durable record was "
                "NOT written, so get_status/get_outcome will report this "
                "operation as unknown even though it ran", operation_id)
            return False
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("outcome_store_persist_failed id=%s err=%s", operation_id, exc)
        return False


async def _supabase_fetch(operation_id: str) -> Optional[dict]:
    """Fetch one row from the operations table.

    Returns None ONLY for a genuine miss -- the store was reached and
    confirmed there is no such row. Raises OutcomeStoreUnavailable for
    everything else: missing config, a network error, a non-2xx response,
    or a 200 whose body was not JSON.

    FIX (2026-09-21): this used to call the lenient `select_rows`, which is
    contractually incapable of raising and returns [] on a missing Supabase
    config exactly as it does on a real empty result (see
    storage/supabase_client.py's own docstring on why the *_strict variants
    exist -- the same fix already shipped this way for core/capture_lead.py,
    agent_interface/key_request_logic.py and core/screen_sanctions.py). That
    made an unreachable store indistinguishable from a genuinely unknown
    operation_id here too, which is the defect get_async()/handle_get_status/
    handle_get_outcome exist to no longer have.

    ROUTED THROUGH RPC, NOT THE RAW TABLE (board row 206 item 1, 2026-09-22):
    this service is deployed with ONLY SUPABASE_ANON_KEY -- no service-role
    key reaches this internet-facing box (see
    sql/agentbroker/001_operations_security_definer_rpc.sql). The anon role
    has NO grant on `operations` at all, so a raw `/rest/v1/operations` call
    would 401/403 for every request, hit or miss alike -- useless for
    telling the two apart. `operations_get_by_id` is a narrow, SECURITY
    DEFINER RPC granted to anon that can only ever return the single row
    named by its one parameter; that boundary is what makes it safe to grant
    to the same key the public internet can drive. `rpc()` itself already
    RAISES on any non-2xx/transport/decode failure (its own contract, used
    today by every spend-gated call) -- exactly the strict semantics this
    function needs, so there is no separate *_strict variant to reach for
    here.
    """
    from storage.supabase_client import rpc
    try:
        result = await rpc("operations_get_by_id", {"p_operation_id": operation_id})
    except Exception as exc:  # noqa: BLE001 - rpc() only ever raises
        # RuntimeError, but nothing here may let ANY unexpected failure
        # masquerade as "no such row" -- see the module docstring on
        # OutcomeStoreUnavailable.
        logger.warning(
            "outcome_store_fetch_unavailable id=%s err=%s -- reporting "
            "unavailable, NOT not_found", operation_id, exc)
        raise OutcomeStoreUnavailable(str(exc)) from exc
    return result or None


async def _supabase_fetch_by_appointment_id(appointment_id: str) -> Optional[dict]:
    """Fetch the durable row that confirmed this PROVIDER booking id.

    Returns None on any error, and also -- correctly -- for a row written
    before the `appointment_id` column existed: that is indistinguishable
    from "no such booking" here, and get_appointment_owner_async resolves
    both the same way (fail closed), same as an operation row with no
    recorded owner at all.

    Routed through the operations_get_by_appointment_id RPC, same reason as
    _supabase_fetch above: the anon key this container deploys with has no
    direct grant on `operations` (board row 206 item 1). This caller never
    distinguished "could not check" from "no such booking" even before that
    change (both fail closed, on purpose -- see get_appointment_owner_async),
    so it stays lenient here; only the transport changed.
    """
    try:
        from storage.supabase_client import rpc
        result = await rpc("operations_get_by_appointment_id",
                            {"p_appointment_id": appointment_id})
        return result or None
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "outcome_store_appointment_fetch_failed id=%s err=%s",
            appointment_id, exc)
        return None


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_store = OutcomeStore()


def get_outcome_store() -> OutcomeStore:
    return _store
