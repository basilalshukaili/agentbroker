"""The x402 rail must settle only what the receipt actually charges.

THE BUG (external review, 2026-09-02). The x402 SDK's ONE settlement gate is
`result.is_error`:

    x402/mcp/server_async.py:261-267
        # If tool returned error, don't settle
        if result.is_error:
            return result
        settle_result = await resource_server.settle_payment(...)

It never reads `receipt.cost.amount`. So every NON-FAILURE receipt settled the
flat quoted price:

  * a `capture_lead` dedup replay - cost 0.00, basis `no_charge_duplicate`,
    the receipt's own text saying "nothing was charged" - took $0.05 of USDC;
  * a `schedule_appointment` receipt reading "NOT BOOKED ... nothing was
    reserved and nothing was charged" took $0.15.

Both are refunds on the credits rail (`run_metered_tool` commits
`cost.amount`, so a zero commits zero). The receipt was right; the rail was
wrong, and the two rails disagreed about the same call.

THE FIX. The SDK exposes no per-response "verify but do not settle" flag and
no settlement callback that can veto - but verification (server_async.py:192)
is already separate from settlement (:267). Raising `is_error` for the
no-charge cases is therefore exactly a verify-without-settle: the EIP-3009
authorization is never submitted, so no USDC moves and the payload is not even
consumed. `run_paid_tool` then restores `isError: false` before the agent sees
it, so the agent is told the truth on both counts: the call succeeded, and it
was not charged.

These tests drive the REAL SDK wrapper (`x402.mcp.server_async`) against a
stub resource server, so they fail if the SDK's settlement condition ever
moves. Nothing here touches the network.
"""
from __future__ import annotations

import asyncio
import time

import pytest
from x402.schemas import PaymentPayload, PaymentRequirements, SettleResponse

import billing.x402_gate as gate
from billing.pricing import price_usd_str


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# The no-charge predicate
# ---------------------------------------------------------------------------

class TestReceiptIsNoCharge:
    def test_dedup_replay_is_no_charge(self):
        assert gate._receipt_is_no_charge({
            "status": "success",
            "reason_code": "lead_already_captured",
            "cost": {"amount": 0.0, "currency": "USD", "basis": "no_charge_duplicate"},
        }) is True

    def test_not_booked_is_no_charge(self):
        assert gate._receipt_is_no_charge({
            "status": "success",
            "reason_code": "requested_time_unavailable",
            "cost": {"amount": 0.0, "currency": "USD", "basis": "no_charge"},
        }) is True

    def test_real_charge_is_chargeable(self):
        assert gate._receipt_is_no_charge({
            "status": "success",
            "reason_code": "lead_captured",
            "cost": {"amount": 0.05, "currency": "USD", "basis": "per_lead"},
        }) is False

    def test_amount_beats_basis(self):
        """cost.amount is what the credits rail settles, so it decides here too.

        A basis that says 'free' next to a real amount is a mislabel, not an
        instruction to give the call away - core/find_business.py has exactly
        that shape."""
        assert gate._receipt_is_no_charge({
            "cost": {"amount": 0.15, "currency": "USD", "basis": "free"},
        }) is False

    def test_basis_decides_when_the_amount_is_missing(self):
        assert gate._receipt_is_no_charge({"cost": {"basis": "no_charge_demo"}}) is True
        assert gate._receipt_is_no_charge({"cost": {"basis": "free"}}) is True
        assert gate._receipt_is_no_charge({"cost": {"basis": "per_lead"}}) is False

    def test_unparseable_amount_falls_back_to_basis(self):
        assert gate._receipt_is_no_charge(
            {"cost": {"amount": "n/a", "basis": "no_charge"}}) is True

    def test_no_cost_block_is_not_a_giveaway(self):
        """Undeterminable is not zero. A receipt that reports no cost at all
        keeps the previous behaviour (chargeable) rather than silently going
        free."""
        assert gate._receipt_is_no_charge({"status": "success"}) is False
        assert gate._receipt_is_no_charge({"status": "success", "cost": None}) is False
        assert gate._receipt_is_no_charge("not a receipt") is False

    def test_the_real_capture_lead_dedup_receipt(self):
        """Built by core/capture_lead.py itself, not a hand-written fixture."""
        from core.capture_lead import _captured
        from core.models import CaptureLeadRequest, ProspectData

        req = CaptureLeadRequest(smb_id="smb_001",
                                 prospect=ProspectData(name="A"), source="test")
        receipt = _captured(
            "op_1", req, "lead_1", "dedup_1", "agentbroker_funnel", None,
            deduplicated=True, t0=time.monotonic(), trace_id=None,
        ).model_dump(mode="json")
        assert receipt["status"] == "success"
        assert gate._receipt_is_error(receipt) is False
        assert gate._receipt_is_no_charge(receipt) is True

    def test_the_real_capture_lead_fresh_receipt_still_charges(self):
        from core.capture_lead import _captured
        from core.models import CaptureLeadRequest, ProspectData

        req = CaptureLeadRequest(smb_id="smb_001",
                                 prospect=ProspectData(name="A"), source="test")
        receipt = _captured(
            "op_1", req, "lead_1", "dedup_1", "agentbroker_funnel", None,
            deduplicated=False, t0=time.monotonic(), trace_id=None,
        ).model_dump(mode="json")
        assert gate._receipt_is_no_charge(receipt) is False


# ---------------------------------------------------------------------------
# End to end through the REAL x402 SDK wrapper
# ---------------------------------------------------------------------------

_REQS = PaymentRequirements(
    scheme="exact", network=gate.MAINNET, asset="0xUSDC",
    amount="50000", pay_to="0xReceiver", max_timeout_seconds=60,
)


class _VerifyResult:
    def __init__(self, valid: bool = True):
        self.is_valid = valid
        self.invalid_reason = None if valid else "bad signature"


class StubResourceServer:
    """Stands in for x402ResourceServer at the facilitator boundary.

    Records every settle_payment call: settlement is the thing under test, so
    the assertion is literally "did the SDK reach the settle boundary".
    """

    def __init__(self):
        self.settlements: list[PaymentRequirements] = []
        self.verifications: list[PaymentPayload] = []

    def build_payment_requirements(self, _cfg):
        return [_REQS]

    def find_matching_requirements(self, accepts, _payload):
        return accepts[0]

    async def verify_payment(self, payload, _reqs):
        self.verifications.append(payload)
        return _VerifyResult(True)

    async def settle_payment(self, _payload, reqs):
        self.settlements.append(reqs)
        return SettleResponse(success=True, transaction="0xf00d", network=reqs.network)

    async def create_payment_required_response(self, accepts, _resource, error, _ext=None):
        from x402.schemas import PaymentRequired
        return PaymentRequired(x402_version=2, accepts=list(accepts), error=error)


@pytest.fixture
def rail(monkeypatch):
    """A wired x402 rail whose only stub is the facilitator boundary."""
    import config

    srv = StubResourceServer()
    monkeypatch.setattr(config, "X402_RECEIVER_ADDRESS", "0xReceiver", raising=False)
    monkeypatch.setattr(config, "X402_PUBLIC_MCP_URL", "https://hatchloop.dev/mcp",
                        raising=False)
    monkeypatch.setattr(config, "X402_ENABLE_TESTNET", False, raising=False)

    async def _server():
        return srv

    settlement_alerts: list = []

    async def _alert(ctx):
        settlement_alerts.append(ctx)

    async def _no_intent(_tool):
        return None

    monkeypatch.setattr(gate, "_ensure_server", _server)
    monkeypatch.setattr(gate, "_notify_first_payment", _alert)
    monkeypatch.setattr(gate, "_notify_buyer_intent", _no_intent)
    monkeypatch.setattr(gate, "_wrappers", {})
    monkeypatch.setattr(gate, "_accepts_cache", {})

    srv.settlement_alerts = settlement_alerts
    return srv


def _paid_meta() -> dict:
    payload = PaymentPayload(payload={"signature": "0xsig"}, accepted=_REQS)
    return {"x402/payment": payload.model_dump(by_alias=True)}


def _call(tool: str, receipt, meta=None) -> dict:
    async def _dispatch():
        return receipt
    return run(gate.run_paid_tool(tool, {}, meta if meta is not None else _paid_meta(),
                                  _dispatch))


_DEDUP_REPLAY = {
    "status": "success",
    "reason_code": "lead_already_captured",
    "human_message": "This prospect was already in the funnel. "
                     "Nothing was duplicated and nothing was charged.",
    "cost": {"amount": 0.0, "currency": "USD", "basis": "no_charge_duplicate"},
}

_NOT_BOOKED = {
    "status": "success",
    "reason_code": "requested_time_unavailable",
    "human_message": "NOT BOOKED: nothing was reserved and nothing was charged.",
    "cost": {"amount": 0.0, "currency": "USD", "basis": "no_charge"},
}

_FRESH_LEAD = {
    "status": "success",
    "reason_code": "lead_captured",
    "human_message": "Lead stored.",
    "cost": {"amount": 0.05, "currency": "USD", "basis": "per_lead"},
}

_FAILED = {
    "status": "failure",
    "reason_code": "supply_unreachable",
    "human_message": "No such SMB.",
    "cost": {"amount": 0.0, "currency": "USD", "basis": "no_charge"},
}


class TestZeroCostSuccessIsNotSettled:
    def test_dedup_replay_settles_nothing(self, rail):
        out = _call("capture_lead", _DEDUP_REPLAY)
        assert rail.settlements == [], (
            "a dedup replay that charges 0.00 on the credits rail must not "
            "take USDC here")
        assert rail.verifications, "the payment must still be VERIFIED"

    def test_dedup_replay_is_still_reported_as_a_success(self, rail):
        out = _call("capture_lead", _DEDUP_REPLAY)
        assert out["isError"] is False, (
            "the is_error flag is how the SDK is told not to settle; the agent "
            "must still be told the truth - the call succeeded")
        assert out["structuredContent"]["status"] == "success"
        assert out["structuredContent"]["reason_code"] == "lead_already_captured"

    def test_dedup_replay_annotation_says_zero(self, rail):
        out = _call("capture_lead", _DEDUP_REPLAY)
        x402 = out["structuredContent"]["x402"]
        assert x402["settled"] is False
        assert x402["settled_usd"] == "0.00"
        assert x402["quoted_usd"] == price_usd_str("capture_lead")
        assert "not charged" in x402["note"].lower()

    def test_not_booked_settles_nothing(self, rail):
        out = _call("schedule_appointment", _NOT_BOOKED)
        assert rail.settlements == []
        assert out["isError"] is False
        assert out["structuredContent"]["x402"]["settled_usd"] == "0.00"

    def test_no_settlement_alert_on_a_free_call(self, rail):
        _call("capture_lead", _DEDUP_REPLAY)
        assert rail.settlement_alerts == [], (
            "the founder's 'a buyer PAID' push must not fire when nothing was "
            "paid")

    def test_no_settlement_metadata_is_invented(self, rail):
        out = _call("capture_lead", _DEDUP_REPLAY)
        meta = out.get("_meta") or {}
        assert "x402/payment-response" not in meta, (
            "there is no settlement, so there must be no settlement receipt")

    def test_the_content_text_matches_the_structured_receipt(self, rail):
        import json
        out = _call("capture_lead", _DEDUP_REPLAY)
        text = json.loads(out["content"][0]["text"])
        assert text["x402"]["settled_usd"] == "0.00"


class TestRealChargeStillSettles:
    def test_fresh_lead_settles_the_quoted_price(self, rail):
        out = _call("capture_lead", _FRESH_LEAD)
        assert len(rail.settlements) == 1, "a real charge must settle exactly once"
        assert rail.settlements[0].amount == _REQS.amount
        assert out["isError"] is False

    def test_fresh_lead_annotation_is_honest(self, rail):
        out = _call("capture_lead", _FRESH_LEAD)
        x402 = out["structuredContent"]["x402"]
        assert x402["settled"] is True
        assert x402["settled_usd"] == price_usd_str("capture_lead")
        assert "0.05" in x402["note"]

    def test_settlement_receipt_reaches_the_agent(self, rail):
        out = _call("capture_lead", _FRESH_LEAD)
        assert out["_meta"]["x402/payment-response"]["transaction"] == "0xf00d"

    def test_settlement_alert_fires_on_a_real_payment(self, rail):
        _call("capture_lead", _FRESH_LEAD)
        assert len(rail.settlement_alerts) == 1


class TestFailureStillSettlesNothing:
    def test_failed_receipt_is_not_settled(self, rail):
        out = _call("capture_lead", _FAILED)
        assert rail.settlements == []
        assert out["isError"] is True, (
            "a failure must still reach the agent as a failure")
        assert out["structuredContent"]["status"] == "failure"

    def test_failed_receipt_carries_no_x402_annotation(self, rail):
        out = _call("capture_lead", _FAILED)
        assert "x402" not in out["structuredContent"]


class TestPaymentRequiredIsNeverRelabelled:
    def test_an_unpaid_call_still_gets_a_402(self, rail):
        """The isError fixup is guarded on receipt identity. A 402 the SDK
        builds itself must never come back as a success."""
        out = _call("capture_lead", _DEDUP_REPLAY, meta={})
        assert rail.settlements == []
        assert out["isError"] is True
        assert out["structuredContent"].get("x402Version") == 2
        assert out["structuredContent"].get("accepts")


class TestTheSDKGateIsWhereWeThinkItIs:
    def test_settlement_is_gated_on_is_error_only(self):
        """If the SDK grows a real 'do not settle' flag, this fails and the
        workaround above should be replaced by it."""
        import inspect
        from x402.mcp import server_async

        src = inspect.getsource(server_async.create_payment_wrapper)
        assert "if result.is_error:" in src
        idx_gate = src.index("if result.is_error:")
        idx_settle = src.index("settle_payment")
        assert idx_gate < idx_settle, (
            "the is_error check must still precede settlement")
