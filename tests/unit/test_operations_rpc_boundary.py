"""The anon-key RPC boundary (board row 206, item 1): "no service-role key on
a public box", enforced via SECURITY DEFINER RPCs scoped to exactly the
reads/writes storage/outcome_store.py needs against `operations`.

TWO KINDS OF PROOF, both here (the live network proof is a separate, opt-in
file -- see tests/integration/test_operations_rpc_boundary_live.py):

1. STRUCTURAL (parses sql/agentbroker/001_operations_security_definer_rpc.sql):
   every operations_* function is SECURITY DEFINER, EXECUTE is granted to
   anon, and the raw table has anon/authenticated/public revoked. This is a
   proof about the migration FILE, not about what a live database actually
   enforces -- see memory postgres-guards-that-do-not-guard (FORCE RLS
   silently running views as OWNER): a migration that reads correctly can
   still be misapplied or superseded, which is exactly why
   tests/integration/test_operations_rpc_boundary_live.py exists alongside
   this one, not instead of it.

2. BEHAVIOURAL (drives the real storage.outcome_store code): patches the
   table-level Supabase functions (select_rows, select_rows_strict,
   upsert_row, insert_row) to raise if called AT ALL, provides a working
   `rpc` fake, and proves get_status/get_outcome/set_complete_durable still
   function correctly end to end. If _supabase_fetch/_supabase_upsert/
   _supabase_fetch_by_appointment_id ever regress to touching the raw table
   endpoint again, this fails immediately -- it does not depend on a live
   database rejecting the call to catch the regression.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

import storage.supabase_client as sb
from storage.outcome_store import OutcomeStore

_AGENTBROKER_REPO = Path(__file__).resolve().parents[2]
# sql/agentbroker/ lives ONE level above the agentbroker git repo, at the
# hatchloop project root -- deliberately (see sql/company/README.md): this
# workspace is not a git repo, so company/product DB internals stay out of
# the public agentbroker GitHub repo by living here instead.
_HATCHLOOP_ROOT = _AGENTBROKER_REPO.parent
_MIGRATION = _HATCHLOOP_ROOT / "sql" / "agentbroker" / "001_operations_security_definer_rpc.sql"

_RPC_FUNCTIONS = (
    "operations_get_by_id",
    "operations_get_by_appointment_id",
    "operations_upsert",
)


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 1. STRUCTURAL -- the migration file itself.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def migration_sql() -> str:
    if not _MIGRATION.is_file():
        # sql/agentbroker/ is deliberately OUTSIDE this git repo (see
        # sql/company/README.md: company/product DB internals stay out of
        # the public agentbroker GitHub repo). A standalone checkout of just
        # `agentbroker/` -- e.g. the public repo's own CI -- will never have
        # it; skip rather than fail so this test is only meaningful (and
        # only enforced) from inside the full hatchloop workspace, where it
        # runs every time this file does.
        pytest.skip(
            f"{_MIGRATION} not found -- only present inside the full "
            f"hatchloop workspace (sql/ is intentionally outside the public "
            f"agentbroker repo); skipping the structural migration checks."
        )
    return _MIGRATION.read_text(encoding="utf-8")


def test_anon_has_no_direct_grant_on_the_table(migration_sql):
    assert re.search(
        r"revoke\s+all\s+on\s+operations\s+from\s+[^;]*\banon\b", migration_sql, re.I
    ), "expected a `revoke all on operations from ... anon ...` statement"
    # And nothing later in the same file re-grants it back to anon.
    grants_to_anon_on_table = re.findall(
        r"grant\s+[^;]*\son\s+operations\s[^;]*to\s+[^;]*\banon\b", migration_sql, re.I
    )
    assert not grants_to_anon_on_table, (
        f"found a GRANT ... ON operations ... TO anon statement -- this "
        f"reopens the exact door the REVOKE above closes: {grants_to_anon_on_table}"
    )


@pytest.mark.parametrize("fn", _RPC_FUNCTIONS)
def test_each_rpc_is_security_definer(migration_sql, fn):
    # Find this function's own CREATE ... AS $$ ... $$ block and confirm
    # SECURITY DEFINER appears between its signature and its body.
    m = re.search(
        rf"create (?:or replace )?function public\.{re.escape(fn)}\(.*?\$\$;",
        migration_sql, re.I | re.S,
    )
    assert m, f"could not find a CREATE FUNCTION block for {fn}"
    assert re.search(r"security\s+definer", m.group(0), re.I), (
        f"{fn} is not declared SECURITY DEFINER -- it would run with the "
        f"CALLER's (anon's) privileges, and RLS would filter it to nothing "
        f"for every caller, silently -- see the module docstring's "
        f"'guard that doesn't guard' note."
    )
    assert re.search(r"set\s+search_path\s*=\s*public", m.group(0), re.I), (
        f"{fn} does not pin search_path -- a SECURITY DEFINER function "
        f"without this is vulnerable to search_path hijacking."
    )


@pytest.mark.parametrize("fn", _RPC_FUNCTIONS)
def test_each_rpc_grants_execute_to_anon(migration_sql, fn):
    assert re.search(
        rf"grant\s+execute\s+on\s+function\s+public\.{re.escape(fn)}\([^)]*\)\s*"
        rf"to\s+[^;]*\banon\b",
        migration_sql, re.I,
    ), f"expected `grant execute on function public.{fn}(...) to ... anon ...`"


# ---------------------------------------------------------------------------
# 2. BEHAVIOURAL -- the real code, with the raw table transport forbidden.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _fresh_store(monkeypatch):
    import storage.outcome_store as os_mod
    fresh = OutcomeStore()
    monkeypatch.setattr(os_mod, "_store", fresh)
    return fresh


@pytest.fixture(autouse=True)
def _forbid_raw_table_access(monkeypatch):
    """The anon key this service deploys with has NO grant on `operations`
    (see test_anon_has_no_direct_grant_on_the_table above) -- so if
    production code ever calls the raw table transport for this table
    again, it would 401/403 in reality. Simulate that HERE, unconditionally,
    so a regression fails a fast, offline unit test instead of only being
    caught by a live 403 against a real database."""
    async def _forbidden(*a, **kw):
        raise AssertionError(
            "the anon-key deploy target has no grant on `operations` -- this "
            "call must go through an operations_* RPC, not the raw table "
            "transport (board row 206 item 1)"
        )

    for name in ("select_rows", "select_rows_strict", "upsert_row", "insert_row"):
        monkeypatch.setattr(sb, name, _forbidden)


class _FakeOperationsTable:
    """A minimal in-memory stand-in for the `operations` table, driven ONLY
    through the three RPC names -- see storage/outcome_store.py's
    _supabase_fetch / _supabase_upsert / _supabase_fetch_by_appointment_id."""

    def __init__(self):
        self.rows: dict[str, dict] = {}

    async def rpc(self, fn, payload):
        if fn == "operations_upsert":
            row = {
                "operation_id": payload["p_operation_id"],
                "tool": payload["p_tool"],
                "status": payload["p_status"],
                "reason_code": payload["p_reason_code"],
                "appointment_id": payload["p_appointment_id"],
                "result_json": payload["p_result_json"],
                "agent_id": payload["p_agent_id"],
            }
            self.rows[row["operation_id"]] = row
            return dict(row)
        if fn == "operations_get_by_id":
            row = self.rows.get(payload["p_operation_id"])
            return dict(row) if row else None
        if fn == "operations_get_by_appointment_id":
            for row in self.rows.values():
                if (row.get("appointment_id") == payload.get("p_appointment_id")
                        and row.get("reason_code") == "appointment_confirmed"):
                    return dict(row)
            return None
        raise AssertionError(f"unexpected rpc fn: {fn!r}")


def test_get_async_resolves_a_cross_process_row_through_rpc_only(monkeypatch):
    """A genuine cross-process read (fresh OutcomeStore, no in-memory hit)
    must still resolve a real row -- proving the RPC-only path is not just
    "safe" but actually functional -- while every raw-table function stays
    forbidden (see _forbid_raw_table_access)."""
    table = _FakeOperationsTable()
    monkeypatch.setattr(sb, "rpc", table.rpc)

    writer = OutcomeStore()
    _run(writer.set_complete_durable(
        "op_boundary_1", {"status": "success", "reason_code": "ok"},
        tool="schedule_appointment", agent_id="agent_x"))

    assert "op_boundary_1" in table.rows, "the write never reached the fake RPC table"

    reader = OutcomeStore()  # a different process: empty in-memory cache
    resolved = _run(reader.get_async("op_boundary_1"))
    assert resolved is not None
    assert resolved["status"] == "success"
    assert resolved["agent_id"] == "agent_x"


def test_genuine_miss_via_rpc_returns_none_not_an_error(monkeypatch):
    table = _FakeOperationsTable()
    monkeypatch.setattr(sb, "rpc", table.rpc)

    reader = OutcomeStore()
    resolved = _run(reader.get_async("op_never_written"))
    assert resolved is None


def test_unreachable_rpc_raises_outcome_store_unavailable_not_a_silent_miss(monkeypatch):
    from storage.outcome_store import OutcomeStoreUnavailable

    async def _down(fn, payload):
        raise RuntimeError("simulated: anon key lacks EXECUTE, or transport down")

    monkeypatch.setattr(sb, "rpc", _down)

    reader = OutcomeStore()
    with pytest.raises(OutcomeStoreUnavailable):
        _run(reader.get_async("op_whatever"))
