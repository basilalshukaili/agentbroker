"""get_status / get_outcome must not tell a caller an operation does not
exist when the truth is the store could not be checked.

THE DEFECT, measured on the live service 2026-09-21. The production
container had NO Supabase configuration at all (`docker exec printenv`:
SUPABASE_URL, SUPABASE_SERVICE_KEY, SUPABASE_SERVICE_ROLE_KEY,
SUPABASE_ANON_KEY all absent). storage/outcome_store.py's Supabase fallback
(`_supabase_fetch`) called the lenient `select_rows`, which is
contractually incapable of raising and returns [] on a missing config
exactly as it does on a real empty result -- see
storage/supabase_client.py's own docstring on why the `*_strict` variants
exist. So get_status/get_outcome answered

    {"status": "not_found", "error": "No operation found with this ID."}

for operation 9ce11af6-a07d-489a-ac0a-5f0b3c6bda35, which EXISTS in the
`operations` table -- a false statement of fact about the service's own
completed work.

THE FIX. `_supabase_fetch` raises `OutcomeStoreUnavailable` instead of
silently swallowing a lookup it could not perform into `None`; the exception
propagates out of `get_async()` rather than being caught there.
`core/status_outcome.py`'s two handlers catch `OutcomeStoreUnavailable` and
answer a distinguishable, retriable service error -- never `not_found`. A
genuine miss (the store IS reachable and confirms no such row) still returns
`None` from `get_async()`, and the two handlers still answer exactly today's
`not_found` shape.

TRANSPORT (board row 206, 2026-09-22): `_supabase_fetch` now calls the
`operations_get_by_id` SECURITY DEFINER RPC
(`storage.supabase_client.rpc`), not `select_rows_strict` against the raw
table -- this service deploys with ONLY the Supabase anon key (no
service-role key on a public box; see
sql/agentbroker/001_operations_security_definer_rpc.sql), and the anon role
has no grant on `operations` at all, so the raw table endpoint is not a
usable transport here any more. `rpc()` already raises on any non-2xx,
transport, or JSON-decode failure -- the same strict contract
`select_rows_strict` gave before -- so these tests mock `sb.rpc` instead:
raising simulates "could not even ask", returning `None` simulates the
SECURITY DEFINER function itself running fine and finding no row (SQL NULL
-> JSON `null`), a genuine miss.

Two tests, both directions -- the second is the control. Without it, always
answering "error" (never checking anything) would also satisfy the first.
"""
from __future__ import annotations

import asyncio

import pytest

import storage.supabase_client as sb
from core.models import OperationStatus
from core.status_outcome import handle_get_status, handle_get_outcome
from storage.outcome_store import OutcomeStore


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _fresh_store(monkeypatch):
    """A brand-new, empty in-memory store, wired as the module singleton --
    so get_async() always falls through to the Supabase layer under test,
    never resolves from a previous test's in-memory cache or leaks into a
    later one."""
    import storage.outcome_store as os_mod
    fresh = OutcomeStore()
    monkeypatch.setattr(os_mod, "_store", fresh)
    return fresh


_REAL_OP_ID = "9ce11af6-a07d-489a-ac0a-5f0b3c6bda35"  # the id from the report


# ---------------------------------------------------------------------------
# Direction 1 (THE DEFECT): the store cannot be reached.
# ---------------------------------------------------------------------------

def test_unreachable_store_is_not_reported_as_not_found(monkeypatch):
    async def _down(fn, payload):
        raise RuntimeError(
            "simulated: rpc() transport/permission failure -- no Supabase "
            "config, network unreachable, or the anon key lacks EXECUTE")

    monkeypatch.setattr(sb, "rpc", _down)

    status_out = _run(handle_get_status(_REAL_OP_ID))
    assert status_out["status"] != "not_found", (
        "an UNREACHABLE store was reported as not_found -- the exact "
        "production defect (measured 2026-09-21 for this operation_id)")
    assert status_out.get("reason_code") not in (None, "not_found")
    assert status_out.get("retriable") is True
    assert status_out["operation_id"] == _REAL_OP_ID

    outcome = _run(handle_get_outcome(_REAL_OP_ID))
    assert outcome.reason_code != "not_found", (
        "an UNREACHABLE store was reported as not_found via get_outcome too")
    assert outcome.status != OperationStatus.SUCCESS
    assert outcome.retriable is True
    assert outcome.cost.amount == 0.0, "get_outcome is free; never charge for an error"


# ---------------------------------------------------------------------------
# Direction 2 (THE CONTROL): the store IS reachable and genuinely has no
# such row. Without this test, satisfying direction 1 by making EVERY lookup
# an error -- never distinguishing the two states at all -- would also pass.
# ---------------------------------------------------------------------------

def test_reachable_store_with_no_matching_row_is_still_not_found(monkeypatch):
    async def _empty(fn, payload):
        return None  # the RPC ran fine and the SQL function returned NULL

    monkeypatch.setattr(sb, "rpc", _empty)

    status_out = _run(handle_get_status("op_genuinely_unknown"))
    assert status_out == {
        "operation_id": "op_genuinely_unknown",
        "status": "not_found",
        "error": "No operation found with this ID.",
    }, "a reachable store's genuine miss must return EXACTLY today's shape"

    outcome = _run(handle_get_outcome("op_genuinely_unknown"))
    assert outcome.status == OperationStatus.FAILURE
    assert outcome.reason_code == "not_found"
    assert outcome.retriable is False
    assert outcome.human_message == "No operation found with ID op_genuinely_unknown."
