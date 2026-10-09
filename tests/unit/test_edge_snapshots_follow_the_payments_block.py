"""No edge snapshot may contradict the payments block the edge bundle itself carries.

THE DEFECT (third review of feat/x402-honesty-20261004, P3). Only edge/src/snapshots/mcp.json was refreshed
when the payment claims were fixed. The edge worker serves every snapshot from its bundle, so a deploy of it
would publish a descriptor saying "no quota is enforced, no rail is on" next to tool and manifest files that
say "free within the daily quota, then $0.02 per call" and "billed per call via credits". It is not live
today (neither api.hatchloop.dev nor hatchloop.dev answers with x-edge-source), which is why this is a ratchet
and not a wall: the files that are still stale are listed BY NAME with the command that fixes them, and

  * every snapshot NOT on the list must agree with the payments block, so a new contradiction fails the build;
  * every snapshot that IS on the list must still contradict it, so refreshing one without removing it from the
    list fails the build too. The list can only shrink.

Refresh: `python scripts/refresh_edge_snapshots.py --local-routes mcp.json` (descriptor),
`--local-tools` (tools/list), and the plain command against the live origin for the rest, then edit KNOWN_STALE.
"""
from __future__ import annotations

import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SNAP = os.path.join(ROOT, "edge", "src", "snapshots")

# What a snapshot says that is only true while the quota is enforced / credits is a rail.
QUOTA_PROMISE = re.compile(r"(?i)free in quota|free within (?:a|the) daily quota|up to (?:a|the) daily quota|"
                           r"within (?:a|the) daily quota")
CREDITS_OFFER = re.compile(r"(?i)billed per call via credits|\(credits\)|buy credits|top up credits|credit packages?")

# Snapshots that predate the payment-claims fix and have not been refreshed. Origin-derived, so they need the
# live origin to be at the release that carries this change first. Each name must still contradict (see below).
# manifest.json and llms-full.txt left this list on feat/maturity-20261009: that branch had to re-capture them (with
# agents.json and mcp-tools-list.json, `refresh_edge_snapshots.py --local-routes` / `--local-tools`) because the old
# copies published the retired failure modes of three tools and the A2A capability flags it removed.
# anthropic-tools.json, openai-tools.json, llms.txt, mcp-initialize.json removed 2026-10-09: refreshed in the
# 0.2.16 bundle deploy (5c67d75), all four snapshots now agree with the all-off payments block.
KNOWN_STALE: dict[str, str] = {}

_TEXT = (".json", ".txt", ".yaml")


def _payments() -> dict:
    with open(os.path.join(SNAP, "mcp.json"), encoding="utf-8") as f:
        return json.load(f)["payments"]


def _contradictions(name: str, payments: dict) -> list[str]:
    with open(os.path.join(SNAP, name), encoding="utf-8") as f:
        text = f.read()
    found = []
    if payments.get("premium_data_quota_enforced") is False and QUOTA_PROMISE.search(text):
        found.append("promises a quota while premium_data_quota_enforced is false")
    if "credits" not in (payments.get("rails") or []) and CREDITS_OFFER.search(text):
        found.append("offers credits while credits is not a rail")
    return found


def _snapshots() -> list[str]:
    return sorted(n for n in os.listdir(SNAP) if n.endswith(_TEXT))


def test_the_payments_block_in_the_bundle_is_the_all_off_state_this_test_judges_against():
    p = _payments()
    assert p["rails"] == [] and p["premium_data_quota_enforced"] is False and p["status"] == "not_enabled", (
        "the snapshot's payments block changed: after flipping a switch, regenerate every snapshot (this test's "
        "premise, and KNOWN_STALE, were written for the all-off state)")


def test_every_snapshot_off_the_stale_list_agrees_with_the_payments_block():
    p = _payments()
    bad = {n: _contradictions(n, p) for n in _snapshots() if n not in KNOWN_STALE}
    bad = {n: c for n, c in bad.items() if c}
    assert bad == {}, f"edge snapshots contradict the payments block: {bad}"


def test_the_stale_list_only_names_snapshots_that_are_still_stale():
    p = _payments()
    refreshed = [n for n in sorted(KNOWN_STALE) if not _contradictions(n, p)]
    assert refreshed == [], (f"{refreshed} no longer contradict the payments block: delete them from KNOWN_STALE "
                             "(the list is a ratchet, it only shrinks)")


def test_the_stale_list_names_files_that_exist():
    assert sorted(set(KNOWN_STALE) - set(_snapshots())) == []


def test_the_two_snapshots_refreshed_with_this_change_are_clean():
    p = _payments()
    for name in ("mcp.json", "mcp-tools-list.json"):
        assert name not in KNOWN_STALE
        assert _contradictions(name, p) == [], name
