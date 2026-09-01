"""capture_lead must write a real row, or say it did not.

WHAT WAS WRONG (fixed 2026-09-01). capture_lead was on the ACT tool list and
persisted nothing. It returned `lead_<uuid5(dedup_key)>` — a locator that
referred to no record anywhere — with status=partial and a channel name for a
CRM it had never contacted. These tests pin the replacement:

  * the lead_id is the id of a row the database actually returned;
  * the $0.05 is charged on a real insert and on nothing else;
  * a failed write is a FAILURE with cost 0.00 and no id, never a synthetic
    success;
  * a retry resolves to the SAME lead instead of erroring or duplicating.

PATCH SITE. The handler does `from storage.supabase_client import insert_row`
INSIDE the function body, so the name is looked up on the module at call time
and patching `storage.supabase_client.insert_row` is what the caller actually
reads. That is the opposite of the dead-stub trap in
test_a_stubbed_gate_is_actually_stubbed.py, where the consumer had bound the
symbol at import time and the definition-site patch was never seen — the
difference is the import being inside the function, so it is checked below
rather than assumed.
"""
from __future__ import annotations

import asyncio
import inspect
from contextlib import contextmanager

import pytest

import storage.supabase_client as sb
from billing.pricing import receipt_usd
from billing.x402_gate import _receipt_is_error
from core.capture_lead import handle_capture_lead
from core.models import CaptureLeadRequest, OperationStatus, ProspectData
from supply.smb_directory import get_directory

PRICE_USD = receipt_usd("capture_lead")


def run(coro):
    return asyncio.run(coro)


@contextmanager
def real_smb(smb_id: str = "smb_001"):
    """Every directory entry ships as is_demo=True, and the demo guard fires
    before the write. Borrow one as a real business for the duration."""
    smb = get_directory().get(smb_id)
    orig_demo, orig_name = smb.is_demo, smb.name
    try:
        smb.is_demo = False
        smb.name = "Real Test Business"
        yield smb
    finally:
        smb.is_demo = orig_demo
        smb.name = orig_name


def _req(name="Jane Doe", phone="+14045551234", email=None, **kw):
    return CaptureLeadRequest(
        smb_id="smb_001",
        prospect=ProspectData(name=name, phone=phone, email=email, **kw),
        source="agent_test",
    )


class _FakeLeads:
    """A leads table with the live UNIQUE(dedup_key) constraint.

    insert_row's real contract is what matters here: it returns None on ANY
    failure, including a unique violation, so the handler cannot tell a retry
    from an outage without reading back.
    """

    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.inserts: list[dict] = []
        self.selects: list[dict] = []
        self._n = 0

    async def insert_row(self, table, row):
        assert table == "leads"
        self.inserts.append(row)
        if row["dedup_key"] in self.rows:
            return None                       # unique violation, silently
        self._n += 1
        stored = dict(row, id=f"11111111-0000-4000-8000-{self._n:012d}",
                      created_at="2026-09-01T00:00:00+00:00")
        self.rows[row["dedup_key"]] = stored
        return stored

    async def select_rows_strict(self, table, **kw):
        assert table == "leads"
        self.selects.append(kw)
        key = kw["filters"]["dedup_key"]
        row = self.rows.get(key)
        return [row] if row else []


@pytest.fixture
def leads(monkeypatch):
    fake = _FakeLeads()
    monkeypatch.setattr(sb, "insert_row", fake.insert_row)
    monkeypatch.setattr(sb, "select_rows_strict", fake.select_rows_strict)
    return fake


# ---------------------------------------------------------------------------
# The patch site is real (guards these tests against being vacuously green)
# ---------------------------------------------------------------------------

def test_handler_reads_the_storage_module_at_call_time():
    """If the import moves to module scope, every monkeypatch below goes dead
    and this file would keep passing while testing nothing."""
    src = inspect.getsource(handle_capture_lead)
    assert "from storage.supabase_client import" in src, (
        "handle_capture_lead no longer imports storage inside the function - "
        "patching storage.supabase_client is no longer the caller's read site")


# ---------------------------------------------------------------------------
# Guards still run AHEAD of the write
# ---------------------------------------------------------------------------

def test_demo_smb_short_circuits_before_any_write(leads):
    r = run(handle_capture_lead(_req()))
    assert r.status == OperationStatus.FAILURE
    assert r.reason_code == "demo_smb_no_live_booking"
    assert r.cost.amount == 0.0
    assert leads.inserts == [], "a demo capture must not put a row in the funnel"


def test_unknown_smb_never_writes(leads):
    req = CaptureLeadRequest(smb_id="smb_GHOST",
                             prospect=ProspectData(name="Ghost"), source="t")
    r = run(handle_capture_lead(req))
    assert r.status == OperationStatus.FAILURE
    assert r.reason_code == "supply_unreachable"
    assert r.cost.amount == 0.0
    assert leads.inserts == []


# ---------------------------------------------------------------------------
# The real write
# ---------------------------------------------------------------------------

def test_success_returns_the_id_of_the_inserted_row(leads):
    with real_smb():
        r = run(handle_capture_lead(_req(), agent_id="agent_7"))
    stored = list(leads.rows.values())[0]
    assert r.status == OperationStatus.SUCCESS
    assert r.reason_code == "lead_captured"
    assert r.result["lead_id"] == stored["id"]
    assert not _receipt_is_error(r.model_dump(mode="json"))


def test_success_charges_the_price_from_the_pricing_table(leads):
    with real_smb():
        r = run(handle_capture_lead(_req()))
    assert r.cost.amount == PRICE_USD
    assert PRICE_USD > 0, "capture_lead does durable work; a 0 price would be a give-away"


def test_written_row_carries_the_caller_supplied_fields(leads):
    with real_smb():
        run(handle_capture_lead(
            _req(email="jane@example.com", service_interest="haircut",
                 notes="asked for evenings", consent_record_id="consent_42"),
            agent_id="agent_7"))
    row = leads.inserts[0]
    assert row["smb_id"] == "smb_001"
    assert row["prospect_name"] == "Jane Doe"
    assert row["prospect_phone"] == "+14045551234"
    assert row["prospect_email"] == "jane@example.com"
    assert row["agent_id"] == "agent_7"
    # No columns exist for these two; losing them silently would throw away the
    # agent's proof of consent.
    assert "haircut" in row["notes"] and "consent_42" in row["notes"]


def test_channel_used_names_what_actually_happened(leads):
    """The stub reported channel_used='direct_api:calcom' off the SMB's
    advertised channel list, having contacted nothing. The receipt now names
    the store the row went into, and the message says plainly that this is not
    a write into the business's own CRM."""
    with real_smb():
        r = run(handle_capture_lead(_req()))
    assert r.channel_used == "internal:supabase_leads"
    assert r.result["channel_used"] == "internal:supabase_leads"
    assert "not a write into the business's own CRM" in r.human_message


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

def test_repeat_capture_returns_the_same_lead(leads):
    with real_smb():
        first = run(handle_capture_lead(_req()))
        second = run(handle_capture_lead(_req()))
    assert second.status == OperationStatus.SUCCESS
    assert second.reason_code == "lead_already_captured"
    assert second.result["lead_id"] == first.result["lead_id"]
    assert second.result["deduplicated"] is True
    assert len(leads.rows) == 1, "the retry duplicated the prospect"


def test_repeat_capture_is_not_charged_twice(leads):
    with real_smb():
        run(handle_capture_lead(_req()))
        second = run(handle_capture_lead(_req()))
    assert second.cost.amount == 0.0
    assert second.cost.basis == "no_charge_duplicate"


def test_dedup_key_formula_is_unchanged(leads):
    """phone, else email, else name — the live UNIQUE index was built on it."""
    with real_smb():
        run(handle_capture_lead(_req(phone="+1555", email="a@b.c")))
        run(handle_capture_lead(_req(phone=None, email="a@b.c")))
        run(handle_capture_lead(_req(name="Solo", phone=None, email=None)))
    assert set(leads.rows) == {"smb_001|+1555", "smb_001|a@b.c", "smb_001|Solo"}


def test_same_prospect_at_a_different_smb_is_a_different_lead(leads):
    req_b = CaptureLeadRequest(
        smb_id="smb_002",
        prospect=ProspectData(name="Jane Doe", phone="+14045551234"),
        source="agent_test",
    )
    with real_smb("smb_001"), real_smb("smb_002"):
        a = run(handle_capture_lead(_req()))
        b = run(handle_capture_lead(req_b))
    assert a.result["lead_id"] != b.result["lead_id"]


# ---------------------------------------------------------------------------
# Honest failure: no id, no charge
# ---------------------------------------------------------------------------

def test_write_failure_is_a_failure_with_no_lead_id(monkeypatch, leads):
    monkeypatch.setattr(sb, "insert_row", lambda t, r: _none())
    with real_smb():
        r = run(handle_capture_lead(_req()))
    assert r.status == OperationStatus.FAILURE
    assert r.reason_code == "upstream_failure"
    assert r.cost.amount == 0.0
    assert r.result is None, "a failed capture must not hand back an id"
    assert r.retriable is True
    assert _receipt_is_error(r.model_dump(mode="json")) is True


def test_unreachable_store_is_not_reported_as_a_new_lead(monkeypatch, leads):
    """select_rows_strict RAISES when it could not run. "I could not check" must
    not collapse into "there is no such lead" — and must not become a success
    either. Both ways out are a FAILURE that charges nothing."""
    async def _down(table, **kw):
        raise sb.SupabaseUnavailable("leads unreachable")

    monkeypatch.setattr(sb, "insert_row", lambda t, r: _none())
    monkeypatch.setattr(sb, "select_rows_strict", _down)
    with real_smb():
        r = run(handle_capture_lead(_req()))
    assert r.status == OperationStatus.FAILURE
    assert r.cost.amount == 0.0
    assert r.result is None


def test_insert_returning_no_id_falls_back_instead_of_inventing_one(monkeypatch, leads):
    """A 200 with a body carrying no id is not a row we can point at.

    A null id is the sharper case: `str(None)` is "None", a truthy string that
    would have been handed back as a lead locator."""
    async def _idless(table, row):
        return {"dedup_key": row["dedup_key"], "id": None}

    monkeypatch.setattr(sb, "insert_row", _idless)
    with real_smb():
        r = run(handle_capture_lead(_req()))
    assert r.status == OperationStatus.FAILURE
    assert r.cost.amount == 0.0
    assert r.result is None


async def _none():
    return None
