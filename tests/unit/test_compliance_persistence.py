"""The consent gate has DURABLE opt-outs, and the compliance trail and key handshake are really stored.

THE FIELD CASE (audit 2026-09-30, fix 8; cutover doc section 13): the container holds only the `anon`
JWT, which does not bypass RLS. On the spine that meant:
  * `compliance_audit` inserts refused (74 in 21 hours): the "immutable audit trail" was in memory only;
  * `pending_keys` writes refused, and its reads returned "empty" for rows it was not allowed to see, so a
    verification link could never be proven used;
  * `consent_optouts` could not be read at start (OPTOUT_HYDRATION_FAILED, HTTP 403) - the in-memory set
    that core/demand_queue.py, core/schedule_appointment.py and the WhatsApp webhook consult began
    every process EMPTY - and could not be written, so a STOP link or a WhatsApp STOP was never durable.

All of it now goes through the SECURITY DEFINER functions in migrations/spine/009. These tests mock the
transport (no network, no Supabase) and pin: the start-up log, the payloads against the SQL signatures,
the failure semantics ("I could not check" is never "nothing there"), and the SQL's own shape.
"""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

import pytest

from compliance import optout_store as store
from compliance.consent_store import ConsentStore
from storage.supabase_client import SupabaseUnavailable

ROOT = Path(__file__).resolve().parents[2]
SQL = (ROOT / "migrations" / "spine" / "009_keyholder_outcome_logging_and_compliance_rpcs.sql").read_text(
    encoding="utf-8")


def _run(coro):
    return asyncio.run(coro)


def _fn_params(name: str) -> set:
    m = re.search(r"create or replace function public\." + name + r"\((.*?)\)\s*returns", SQL, re.S)
    assert m, f"{name} is not defined in migration 009"
    return set(re.findall(r"\b(p_[a-z_]+)\b", m.group(1)))


@pytest.fixture
def rpc_calls(monkeypatch):
    """Mock the transport; tests set `answer` to a value or an exception."""
    import storage.supabase_client as sb

    class Box:
        calls: list = []
        answer = None

    box = Box()
    box.calls = []

    async def _rpc(fn, payload):
        box.calls.append((fn, payload))
        if isinstance(box.answer, Exception):
            raise box.answer
        if callable(box.answer):
            return box.answer(fn, payload)
        return box.answer
    monkeypatch.setattr(sb, "rpc", _rpc)
    monkeypatch.setattr(sb, "_get_config", lambda: ("https://spine.test", "anon-jwt"))
    return box


# ---------------------------------------------------------------------------
# the opt-out list: load
# ---------------------------------------------------------------------------

def test_load_pages_through_the_whole_list_in_order(rpc_calls):
    full = [{"recipient_id": f"r{i}", "channel": "sms"} for i in range(store.HYDRATE_PAGE)]
    tail = [{"recipient_id": "last", "channel": "email"}]
    rpc_calls.answer = lambda fn, p: full if p["p_offset"] == 0 else tail
    pairs = _run(store.load_durable_optouts())
    assert len(pairs) == store.HYDRATE_PAGE + 1 and pairs[-1] == ("last", "email")
    assert [c[1]["p_offset"] for c in rpc_calls.calls] == [0, store.HYDRATE_PAGE]
    assert all(c[0] == "consent_optouts_hydrate" for c in rpc_calls.calls)


def test_an_unreadable_list_raises_rather_than_returning_empty(rpc_calls):
    rpc_calls.answer = RuntimeError("rpc failed: HTTP 403")
    with pytest.raises(SupabaseUnavailable):
        _run(store.load_durable_optouts())


def test_an_answer_that_is_not_a_list_is_unavailable_not_empty(rpc_calls):
    for bad in ({"error": "x"}, None, "ok", 5):
        rpc_calls.answer = bad
        with pytest.raises(SupabaseUnavailable):
            _run(store.load_durable_optouts())


def test_an_empty_list_is_a_real_answer(rpc_calls):
    rpc_calls.answer = []
    assert _run(store.load_durable_optouts()) == []


# ---------------------------------------------------------------------------
# the START-UP LOG the audit asked for
# ---------------------------------------------------------------------------

def _boot(monkeypatch, caplog):
    """Run the real lifespan hook against a throwaway consent store; return that store."""
    import compliance.consent_store as cs
    import main
    fresh = ConsentStore()
    monkeypatch.setattr(cs, "_store", fresh)

    async def go():
        async with main.lifespan(main.app):
            pass
    with caplog.at_level(logging.INFO, logger="smb_broker"):
        _run(go())
    return fresh


def test_startup_hydrates_the_durable_list_and_logs_no_failure(rpc_calls, monkeypatch, caplog):
    rpc_calls.answer = [{"recipient_id": "+15551230001", "channel": "sms"},
                        {"recipient_id": "stop@example.com", "channel": "email"}]
    store_ = _boot(monkeypatch, caplog)
    assert "OPTOUT_HYDRATION_FAILED" not in caplog.text, caplog.text
    assert "hydrated 2 durable opt-outs" in caplog.text
    assert store_.is_opted_out("+15551230001", "sms") is True
    assert store_.is_opted_out("stop@example.com", "whatsapp") is True, "a STOP covers every channel"
    assert store_.is_opted_out("someone-else@example.com", "email") is False


def test_startup_with_an_unreadable_list_still_boots_and_says_so_loudly(rpc_calls, monkeypatch, caplog):
    """The OLD behaviour, kept for the failure case: never block startup, never read as 'none'."""
    rpc_calls.answer = RuntimeError("rpc('consent_optouts_hydrate') failed: HTTP 403 permission denied")
    store_ = _boot(monkeypatch, caplog)
    assert "OPTOUT_HYDRATION_FAILED" in caplog.text
    assert "WITHOUT durable opt-outs" in caplog.text
    assert store_.is_opted_out("+15551230001", "sms") is False


def test_the_boot_path_no_longer_touches_the_table_directly():
    import inspect
    import main
    src = inspect.getsource(main.lifespan)
    assert "select_rows_strict" not in src and '"consent_optouts"' not in src
    assert "load_durable_optouts" in src


# ---------------------------------------------------------------------------
# the opt-out list: write
# ---------------------------------------------------------------------------

def test_record_optout_calls_the_function_with_its_exact_parameters(rpc_calls):
    rpc_calls.answer = {"recorded": True, "already_present": False}
    out = _run(store.record_optout("+15551230002", "sms", revocation_method="keyword_STOP",
                                   source="inbound_handler"))
    fn, payload = rpc_calls.calls[0]
    assert fn == "consent_optouts_record" and out["recorded"] is True
    assert set(payload) <= _fn_params("consent_optouts_record")
    assert payload["p_recipient_id"] == "+15551230002" and payload["p_use_case"] == "marketing"


def test_record_optout_that_is_not_confirmed_raises(rpc_calls):
    for bad in ({"recorded": False}, {}, None, []):
        rpc_calls.answer = bad
        with pytest.raises(SupabaseUnavailable):
            _run(store.record_optout("a", "sms"))
    rpc_calls.answer = RuntimeError("HTTP 403")
    with pytest.raises(SupabaseUnavailable):
        _run(store.record_optout("a", "sms"))


def test_the_lenient_writer_never_raises_and_does_not_log_the_recipient(rpc_calls, caplog):
    rpc_calls.answer = RuntimeError("HTTP 403")
    with caplog.at_level(logging.ERROR):
        assert _run(store.record_optout_lenient("+96891234567", "sms")) is None
    assert "+96891234567" not in caplog.text
    assert "optout_durable_write_failed" in caplog.text


def test_all_three_stop_writers_use_the_function_not_the_table():
    """The three places that record a STOP. Each used insert_row('consent_optouts', ...) as anon."""
    import inspect
    from agent_interface import unsubscribe, whatsapp_webhook
    from core import handle_inbound
    for mod in (unsubscribe, whatsapp_webhook, handle_inbound):
        src = inspect.getsource(mod)
        assert 'insert_row("consent_optouts"' not in src and 'insert_row_strict("consent_optouts"' not in src, mod
        assert "optout_store" in src, mod


# ---------------------------------------------------------------------------
# compliance_audit
# ---------------------------------------------------------------------------

def test_the_audit_payload_matches_the_function_signature(rpc_calls):
    from compliance.audit_log import AuditLog, AuditEventType
    rpc_calls.answer = {"inserted": True}

    async def go():
        AuditLog().record(AuditEventType.COMPLIANCE_VIOLATION, agent_id="a", decision="block",
                          reason="r", recipient_id="+15551230003", token="tok", metadata={"k": 1})
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    _run(go())
    fn, payload = rpc_calls.calls[0]
    assert fn == "compliance_audit_insert"
    assert set(payload) == _fn_params("compliance_audit_insert")
    assert "+15551230003" not in str(payload) and "tok" != payload["p_token_hash"]


# ---------------------------------------------------------------------------
# pending_keys
# ---------------------------------------------------------------------------

def test_store_pending_reports_whether_it_stored(rpc_calls):
    from agent_interface import key_request_logic as krl
    rpc_calls.answer = {"stored": True}
    assert _run(krl.store_pending("a@example.com", "tok", 1_900_000_000.0)) is True
    fn, payload = rpc_calls.calls[0]
    assert fn == "pending_keys_upsert" and set(payload) <= _fn_params("pending_keys_upsert")
    for bad in (RuntimeError("HTTP 403"), {"stored": False}, None, []):
        rpc_calls.answer = bad
        assert _run(krl.store_pending("a@example.com", "tok", 1_900_000_000.0)) is False


def test_store_pending_is_true_when_no_database_is_configured(monkeypatch):
    """Dev and the test suite have none by design; that must not turn signup into a 503."""
    import storage.supabase_client as sb
    from agent_interface import key_request_logic as krl
    monkeypatch.setattr(sb, "_get_config", lambda: ("", ""))
    assert _run(krl.store_pending("a@example.com", "tok", 1_900_000_000.0)) is True


def test_machine_minted_keys_are_stored_through_the_function(rpc_calls):
    from agent_interface import key_request_logic as krl
    rpc_calls.answer = {"stored": True}
    _run(krl.store_machine_minted("agent-1", "x" * 900, 1_900_000_000.0))
    fn, payload = rpc_calls.calls[0]
    assert fn == "pending_keys_upsert" and payload["p_source"] == "machine_minted"
    assert payload["p_email"] == "agent:agent-1" and len(payload["p_token"]) == 512
    assert set(payload) <= _fn_params("pending_keys_upsert")


def test_consume_payloads_match_the_function(rpc_calls):
    from agent_interface import key_request_logic as krl
    rpc_calls.answer = {"found": True, "email": "a@example.com"}
    _run(krl.consume_pending("tok", email="a@example.com"))
    _run(krl.consume_pending("tok"))
    for fn, payload in rpc_calls.calls:
        assert fn == "pending_keys_consume" and set(payload) <= _fn_params("pending_keys_consume")


def test_a_link_whose_pending_row_could_not_be_stored_is_not_emailed(monkeypatch):
    """Now that consume can prove a row ABSENT, emailing a link we failed to record would hand the
    person a link that is refused as 'already used' on its first click."""
    from agent_interface import key_requests as KR
    sent = []

    async def _no_store(*a, **k):
        return False

    async def _send(*a, **k):
        sent.append(a)
        return True
    monkeypatch.setattr(KR, "store_pending", _no_store)
    monkeypatch.setattr(KR, "send_verification_email", _send)
    resp = _run(KR.request_free_key(body=KR.KeyRequestBody(email="person@example.org")))
    assert resp.status_code == 503
    import json
    assert json.loads(resp.body)["error"] == "onboarding_unavailable"
    assert sent == [], "an email was sent for a verification that was never recorded"


def test_a_stored_pending_row_lets_the_email_go(monkeypatch):
    from agent_interface import key_requests as KR
    sent = []

    async def _store(*a, **k):
        return True

    async def _send(*a, **k):
        sent.append(a)
        return True
    monkeypatch.setattr(KR, "store_pending", _store)
    monkeypatch.setattr(KR, "send_verification_email", _send)
    resp = _run(KR.request_free_key(body=KR.KeyRequestBody(email="person@example.org")))
    assert resp.status_code == 200 and len(sent) == 1


def test_an_unrecognised_consume_answer_is_never_read_as_already_used(rpc_calls):
    from agent_interface import key_request_logic as krl
    for bad in ([], {"found": "yes"}, {"email": "a@example.com"}, None):
        rpc_calls.answer = bad
        with pytest.raises(krl.PendingLookupUnavailable):
            _run(krl.consume_pending("tok", email="a@example.com"))


# ---------------------------------------------------------------------------
# the migration itself
# ---------------------------------------------------------------------------

NEW_FUNCTIONS = ["usage_events_insert_v2", "compliance_audit_insert", "pending_keys_upsert",
                 "pending_keys_consume", "consent_optouts_hydrate", "consent_optouts_record"]


def _function_block(name: str) -> str:
    m = re.search(r"create or replace function public\." + name + r"\(.*?\n\$\$;", SQL, re.S)
    assert m, name
    return m.group(0)


@pytest.mark.parametrize("name", NEW_FUNCTIONS)
def test_every_new_function_is_security_definer_with_a_pinned_search_path(name):
    block = _function_block(name)
    assert "security definer" in block
    assert "set search_path = public" in block, "a SECURITY DEFINER function without a pinned search_path is hijackable"


@pytest.mark.parametrize("name", NEW_FUNCTIONS)
def test_every_new_function_is_revoked_from_the_default_grantees_and_granted_narrowly(name):
    revoke = re.search(r"revoke all on function public\." + name + r"\([^;]*?\)\s+from public, anon, authenticated;",
                       SQL, re.S)
    grant = re.search(r"grant execute on function public\." + name + r"\([^;]*?\)\s+to anon, service_role;",
                      SQL, re.S)
    assert revoke, f"{name}: new functions are granted to anon/authenticated BY DEFAULT on this cluster"
    assert grant, name
    assert "authenticated" not in grant.group(0) and "public" not in grant.group(0).split("to")[-1]


def test_the_migration_is_additive_and_idempotent():
    assert not re.search(r"\bdrop\s+(table|function|column|index)\b", SQL, re.I)
    assert not re.search(r"\btruncate\b|\bdelete\s+from\s+(usage_events|compliance_audit|consent_optouts)\b", SQL, re.I)
    assert not re.search(r"create\s+function", SQL, re.I), "every function must be CREATE OR REPLACE"
    for stmt in re.findall(r"add column[^,;]*", SQL):
        assert "if not exists" in stmt, stmt
    for stmt in re.findall(r"create index[^;]*", SQL):
        assert "if not exists" in stmt, stmt
    assert "usage_events_insert(" not in SQL.replace("usage_events_insert_v2(", ""), (
        "the live 7-argument function must be left alone so the running container keeps working")


def test_no_function_ever_returns_a_stored_token_or_the_audit_rows():
    consume = _function_block("pending_keys_consume")
    assert "returning email" in consume and "returning token" not in consume
    returns = re.findall(r"returns\s+(table\s*\([^)]*\)|jsonb|boolean|integer)", SQL)
    assert "token" not in " ".join(returns).lower()
    assert "select * from pending_keys" not in SQL and "select token" not in SQL.lower()


def test_the_audit_insert_cannot_overwrite_a_row():
    block = _function_block("compliance_audit_insert")
    assert "on conflict (audit_id) do nothing" in block
    assert "do update" not in block and "update compliance_audit" not in block


def test_the_direct_doors_are_closed_for_the_three_tables():
    for t in ("compliance_audit", "pending_keys", "consent_optouts"):
        assert re.search(r"revoke all on public\." + t + r"\s+from anon, authenticated, public;", SQL), t
