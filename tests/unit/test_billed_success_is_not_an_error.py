"""A call the customer paid for must not be reported as failed.

THE BUG. The credits path decided isError like this:

    _is_cr_err = (_cr_result.get("status") == "failure"
                  or "reason_code" in _cr_result)

_dispatch_operation returns receipt.model_dump(), and reason_code is a
DECLARED FIELD on OutcomeReceipt - so the key exists on every result,
including successes where its value is None. The second clause was therefore
always true, and every credits-billed call came back to the MCP client with
isError: true.

Settlement is decided separately and correctly inside run_metered_tool, so
the customer really was charged. Then told it failed. An agent that retries
on isError is billed twice for the same work.

CREDITS_ENABLED is true on the live service, so this was not theoretical.
The free path one screen away had it right - `receipt.get("status") ==
"failure"` - which is what makes this the kind of defect that survives
review: the correct version is visible in the same file.
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))


def _source() -> str:
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "agent_interface", "mcp_server.py")
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def test_reason_code_presence_is_never_used_to_mean_error():
    """The key is always present. Testing for it is testing nothing."""
    # CODE ONLY. The fix's own comment quotes the broken expression to
    # explain it, and a guard that cannot tell an explanation from the thing
    # it explains would force us to delete the explanation.
    offending = [
        (n, line.strip())
        for n, line in enumerate(_source().splitlines(), 1)
        if '"reason_code" in _cr_result' in line
        and not line.strip().startswith("#")
    ]
    assert not offending, (
        f"isError is being decided by whether a declared field EXISTS, which "
        f"is always true - so every paid call reports as failed: {offending}")


def test_a_receipt_always_carries_the_reason_code_key():
    """Guard the guard: if this ever stops being true, the test above is
    protecting against a condition that can no longer occur, and someone
    should know that rather than keep the rule out of superstition."""
    from core.models import OutcomeReceipt, OperationStatus, CostRecord

    receipt = OutcomeReceipt(
        operation_id="op_1",
        status=OperationStatus.SUCCESS,
        reason_code=None,
        human_message="fine",
        result={},
        cost=CostRecord(amount=0.0, currency="USD", basis="free"),
        latency_ms=1,
    )
    dumped = receipt.model_dump()
    assert "reason_code" in dumped
    assert dumped["reason_code"] is None
    assert dumped["status"] != "failure"


def test_both_paths_decide_error_the_same_way():
    """The free path and the credits path must agree on what an error is."""
    src = _source()
    frees = re.findall(r'isError["\']?\s*:\s*([^,\n]+)', src)
    assert frees, "could not find any isError decision"
    for expr in frees:
        assert "reason_code" not in expr, (
            f"isError decided by {expr.strip()!r} - a paid success would be "
            f"reported as a failure")
