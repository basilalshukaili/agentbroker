"""
A safety- or honesty-critical disclosure clause must survive
`_format_description_for_llm`'s truncation TODAY, and stay able to survive
the next unrelated edit -- not just at the moment someone counted characters.

Context (2026-09-22): `import_booking_url`'s description used to claim its
imported smb_id worked "straight to schedule_appointment" for all 12 detected
booking platforms. Only Cal.com -- bound to this deployment's one connected
account -- ever completes a booking; the other 11 always reach an honest,
uncharged failure. That was fixed in manifest/manifest.json, and the
corrected description was then REORDERED so the disclosure sits right after
the opening sentence instead of after a 12-platform name list.

Why the reorder isn't self-enforcing: `_format_description_for_llm`
(agent_interface/mcp_server.py) truncates a raw description from the END at
`_MAX_DESC_CHARS` (450) and appends an ellipsis. The cost tag is appended
AFTER that cut, so the string an agent actually receives can run past 450 --
proven live: import_booking_url currently publishes at 453 chars ("[free,
requires key]" is 21 chars; 432 raw + 21 = 453). That means the budget a raw
description must respect to keep the WHOLE published string under 450 is not
450 -- it is `450 - len(cost tag) - len(async tag)`, which is smaller than
the naive number and specific to each tool's cost model.

Nobody editing this file six months from now will re-derive any of that by
hand. This test does the arithmetic every run, against the RAW description
(before truncation), and fails with the exact numbers the moment a registered
disclosure no longer has room to survive an ordinary edit elsewhere in the
same description.
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent_interface.mcp_server import _format_description_for_llm, _MAX_DESC_CHARS  # noqa: E402

with open(os.path.join(ROOT, "manifest", "manifest.json"), encoding="utf-8") as fh:
    MANIFEST = json.load(fh)
OPS = {o["name"]: o for o in MANIFEST.get("operations", [])}

# Spare characters a registered disclosure must keep between where it ends
# and the REAL ceiling (see module docstring) before an unrelated future edit
# could push it past the cut. Set well below import_booking_url's current
# ~191-char margin so this only trips on a genuine regression, not rounding.
SAFETY_MARGIN_CHARS = 60

# tool name -> exact substring that must (a) exist verbatim in its raw
# description and (b) end with a safety margin before the real truncation
# ceiling. Add an entry whenever a description earns a safety- or
# honesty-critical disclosure -- an unregistered disclosure gets no
# protection from this test.
PROTECTED_DISCLOSURES = {
    "import_booking_url": (
        "But schedule_appointment only completes on Cal.com bound to our "
        "one connected account — the other 11 always fail "
        "schedule_appointment honestly, uncharged."
    ),
}


def _tag_length(op: dict) -> int:
    """Length of the cost/async tag `_format_description_for_llm` appends
    AFTER truncation, for this op's actual cost_model + execution_profile.

    Computed by calling the real production function with the description
    blanked out, rather than re-implementing its cost-tag branches here --
    duplicated logic drifts silently the day someone adds a pricing tier and
    forgets this file exists. With description="", nothing is long enough to
    truncate, so the return value IS the tag.
    """
    probe = dict(op)
    probe["description"] = ""
    return len(_format_description_for_llm(probe))


def test_protected_disclosures_are_present_verbatim():
    """The registered clause must exist, unmodified, in the raw description.

    If this fails, someone reworded or deleted the disclosure directly in
    manifest.json -- a bigger problem than truncation, and worth catching on
    its own rather than folded into the margin assertion below.
    """
    for tool, phrase in PROTECTED_DISCLOSURES.items():
        assert tool in OPS, (
            f"{tool!r} is registered in PROTECTED_DISCLOSURES but no longer "
            f"exists in manifest/manifest.json. Update the registry."
        )
        raw = OPS[tool].get("description", "")
        assert phrase in raw, (
            f"{tool}: the registered disclosure is no longer present "
            f"verbatim in manifest.json's description.\n"
            f"  expected to find: {phrase!r}\n"
            f"  actual description: {raw!r}\n"
            f"Either the honest wording changed (update PROTECTED_DISCLOSURES "
            f"to match the new text, and confirm it is still equally honest) "
            f"or the disclosure was silently dropped from the source file "
            f"(put it back)."
        )


def test_protected_disclosures_have_a_truncation_safety_margin():
    """A registered disclosure must end well before the REAL cap, so a
    future edit anywhere else in the description can't push it past
    `_format_description_for_llm`'s end-truncation and silently delete it.
    """
    for tool, phrase in PROTECTED_DISCLOSURES.items():
        op = OPS[tool]
        raw = op.get("description", "")
        start = raw.find(phrase)
        assert start != -1, (
            f"{tool}: disclosure not found in raw description -- see "
            f"test_protected_disclosures_are_present_verbatim for detail."
        )
        end = start + len(phrase)

        tag_len = _tag_length(op)
        real_ceiling = _MAX_DESC_CHARS - tag_len
        margin = real_ceiling - end

        assert margin >= SAFETY_MARGIN_CHARS, (
            f"\n{tool}: the disclosure\n"
            f"    {phrase!r}\n"
            f"ends at character {end} of its {len(raw)}-char raw description "
            f"(manifest/manifest.json). Once the {tag_len}-char cost/async "
            f"tag is appended AFTER truncation, the real ceiling for this "
            f"tool's raw description is {_MAX_DESC_CHARS} - {tag_len} = "
            f"{real_ceiling} chars, not the naive {_MAX_DESC_CHARS} -- so "
            f"this disclosure has only {margin} spare characters left "
            f"(need >= {SAFETY_MARGIN_CHARS}) before an edit ANYWHERE ELSE "
            f"in the description pushes it past "
            f"agent_interface/mcp_server.py's `_format_description_for_llm` "
            f"end-truncation, the ellipsis lands inside or before this "
            f"clause, and the tool silently goes back to claiming something "
            f"false in every tools/list response -- no test would fail and "
            f"no deploy step would complain. Move the disclosure earlier in "
            f"the description (idempotency notes and similar boilerplate "
            f"belong at the very end, where truncation is safe to eat them "
            f"first), or shorten text ahead of it. Do not fix this by "
            f"raising SAFETY_MARGIN_CHARS or by deleting the registry entry."
        )


def test_no_tool_description_is_ever_truncated():
    """Zero ellipses across the whole surface -- unconditionally, for every
    tool, whether or not anyone registered a disclosure for it.

    Context (2026-09-22): a caller running `tools/list` against the live
    endpoint found 5 of 23 tools -- send_message, call_business,
    check_compliance, screen_sanctions, map_trade_restriction -- with
    descriptions silently cut mid-sentence and terminated with "...". Each
    lost real content: send_message lost the sentence saying a rejected
    marketing send gets a structured compliance_violation receipt;
    screen_sanctions lost "Never fabricates a match or a clear" entirely.
    None of the five had a PROTECTED_DISCLOSURES entry, so the tests above
    -- which only guard a clause someone thought to register -- passed the
    whole time. All five were then rewritten (manifest/manifest.json) with
    the honesty-critical sentence moved early and restatements/examples
    trimmed, so each now fits within its real per-tool ceiling.

    This test does not depend on any registry. It fails the moment ANY
    tool's raw description would be truncated by
    `_format_description_for_llm`'s end-cut at `_MAX_DESC_CHARS`, before
    that ellipsis ever reaches a live caller -- catching every future case,
    not just the five found this way.
    """
    ELLIPSIS = "…"
    failures = []
    for name in sorted(OPS):
        op = OPS[name]
        raw = op.get("description", "")
        published = _format_description_for_llm(op)
        if ELLIPSIS not in published:
            continue
        # Recompute the exact same cut `_format_description_for_llm` makes
        # (raw[:_MAX_DESC_CHARS].rsplit(" ", 1)[0]) so the failure message
        # shows precisely what text a live caller loses, verbatim.
        kept = raw[:_MAX_DESC_CHARS].rsplit(" ", 1)[0]
        lost = raw[len(kept):]
        failures.append(
            f"  {name}: raw description is {len(raw)} chars (ceiling "
            f"{_MAX_DESC_CHARS}) -- truncation silently drops: {lost!r}"
        )
    assert not failures, (
        "The following tool description(s) are truncated with an ellipsis "
        "in the exact string tools/list sends to a real caller. Whatever "
        "text follows the cut -- a disclosure, a limit, an honesty clause "
        "-- is gone from every request this server answers, whether or not "
        "it was ever registered in PROTECTED_DISCLOSURES above:\n"
        + "\n".join(failures)
        + "\nFix by reordering the description so what you cannot afford to "
        "lose sits before the cut (examples, platform lists, idempotency "
        "notes and restatements belong at the very end, where truncation is "
        "safe to eat them first), or by shortening genuinely redundant text "
        "-- never by raising _MAX_DESC_CHARS and never by deleting the "
        "honesty content itself."
    )
