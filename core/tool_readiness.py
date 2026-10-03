"""Which tools are not production-ready, and the one honest sentence about each.

WHY. A tool list that shows 23 tools as equals is a claim that all 23 work. They do not, and a
reviewer who runs "every tool" (Claude's directory review does exactly that) or a daily directory
test finds the difference for us. Until now the only label was for delivery channels that this
deployment does not have (core/channel_status.py: SMS and voice). Other tools are limited in ways a
caller cannot see until a call fails: verify_business cannot verify what find_business returns,
schedule_appointment completes for one connected Cal.com account only, mint_key refuses everyone
without a shared secret, handle_inbound is substring matching.

THE STATES (a tool with no `readiness` entry is production-ready and carries no label):

  beta         Works, against real data or a real store, but with a limit that changes what a caller
               can rely on (unverified community data, a keyword classifier, a queue nothing here reads).
  limited      Works for a NARROW subset of inputs; for everything else it fails honestly, uncharged.
  unavailable  Cannot run on this deployment right now. Set at request time from the environment by
               core/channel_status.py, never typed here.

WHERE THE FACTS LIVE. In manifest/manifest.json, on the operation: `"readiness": {"state", "summary"}`.
tools/list is built from that file, so are the generated catalogues, and one test
(tests/unit/test_tool_readiness.py) reads every surface and fails if one disagrees. A label typed
into three places is a label that is wrong in two of them by next month - the lesson of
core/tool_auth.py, applied again.

THE LABEL IS NOT A SECOND DESCRIPTION. It is a state in brackets after the description (about three
tokens, because the model-context budget in scripts/check_context_budget.py counts every one) and the
sentence in `_meta["hatchloop/readiness"]`, which costs the model nothing. The specifics a caller needs
to plan around stay in the description itself, where they were.
"""
from __future__ import annotations

from typing import Optional

STATES = ("beta", "limited", "unavailable")
# What the manifest may carry. "unavailable" is dynamic only (channel_status), never stored.
STORED_STATES = ("beta", "limited")

META_KEY = "hatchloop/readiness"
_SEVERITY = {"beta": 1, "limited": 2, "unavailable": 3}


def of(op: dict) -> Optional[dict]:
    """The validated readiness entry of a manifest operation, or None (= production-ready).

    A malformed entry raises: a label that silently fails to appear is the failure this file exists
    to prevent, and the manifest is ours to fix."""
    r = op.get("readiness")
    if r is None:
        return None
    if (not isinstance(r, dict) or r.get("state") not in STORED_STATES
            or not isinstance(r.get("summary"), str) or not r["summary"].strip()):
        raise ValueError(f"{op.get('name')}: readiness needs a state in {STORED_STATES} and a non-empty summary")
    return {"state": r["state"], "summary": " ".join(r["summary"].split())}


def tag(state: str) -> str:
    """The bracketed state appended to a description: ' [beta]'."""
    return f" [{state}]"


def labelled_description(op: dict) -> str:
    """op['description'] with its state, for the surfaces that print the raw description."""
    rd = of(op)
    return op.get("description", "") + (tag(rd["state"]) if rd else "")


def stronger(a: Optional[dict], b: Optional[dict]) -> Optional[dict]:
    """The more severe of two readiness entries (None counts as production)."""
    if a is None:
        return b
    if b is None:
        return a
    return a if _SEVERITY[a["state"]] >= _SEVERITY[b["state"]] else b


def from_availability(notice: dict) -> dict:
    """Readiness implied by a channel-availability notice (core/channel_status._tool_notice)."""
    reason = " ".join(str(notice.get("reason") or "A delivery channel is not configured here.").split())
    if notice.get("available") is False:
        return {"state": "unavailable", "summary": reason}
    return {"state": "beta", "summary": "Some delivery channels are not configured here: " + reason}


def all_labelled(operations: list) -> dict:
    """{tool name: state} for every operation that carries a stored label. Used by the discovery
    documents and the tests."""
    out = {}
    for op in operations:
        rd = of(op)
        if rd:
            out[op["name"]] = rd["state"]
    return out
