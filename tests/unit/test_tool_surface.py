"""Tool-surface size assertions — modelled on grocery/tools/surface.py.

THE PROBLEM (measured 2026-09-09, board item 80ab9c53): the live tools/list
response is ~52,500 bytes / ~13,100 tokens.  A phone-resident model running
AgentBroker alongside other servers cannot hold that and still have room for a
conversation.  Founder 1247: "It should not confuse the agent with so many tools
and blow its context window, especially when the agent is on mobile phone."

TARGET (from CEO decision, applying grocery surface pattern):
  MAX_TOOL_LIST_TOKENS = 2,500  (≈ 10,000 bytes at 4 bytes/token)

We are not there yet.  The tests below enforce two things:
  1. A GROWTH CEILING — the list must not get LARGER than it already is.
  2. A REDUCTION TARGET — tested separately, currently skipped until the
     description rewrites land (tracked in board item 80ab9c53).

Adding a test that marks the current state as the ceiling prevents the list
from growing while the rewrite is in progress.
"""
import json
import sys
import os

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from agent_interface.mcp_server import _build_tool_list

# The serialised tools/list must not exceed this.
# Set to the measured 2026-09-09 baseline + 10% headroom so the ceiling is
# real today while the description rewrites are in progress.
MAX_BYTES_CEILING = 58_000  # current ~52,500 + 10% ≈ 57,750 → round up

# The target once descriptions are rewritten.  Currently skipped.
TARGET_BYTES = 10_000  # ~2,500 tokens


def _tool_list_bytes() -> int:
    return sum(len(json.dumps(t).encode()) for t in _build_tool_list())


def test_tool_list_does_not_grow():
    """Growth ceiling: the tool list must not exceed the 2026-09-09 baseline.

    If this test fails, a description was made LONGER.  Trim it back.
    """
    size = _tool_list_bytes()
    assert size <= MAX_BYTES_CEILING, (
        f"tools/list grew to {size:,} bytes (ceiling={MAX_BYTES_CEILING:,}).  "
        f"The list is already too large for phone-resident models; making it "
        f"larger goes in the wrong direction.  See board item 80ab9c53."
    )


@pytest.mark.xfail(
    reason="Descriptions not yet rewritten to the 2,500-token target (board 80ab9c53)"
)
def test_tool_list_meets_target():
    """Reduction target: every description rewritten → list under TARGET_BYTES.

    Remove the xfail mark once the rewrites land.
    """
    size = _tool_list_bytes()
    assert size <= TARGET_BYTES, (
        f"tools/list is {size:,} bytes; target is {TARGET_BYTES:,} bytes "
        f"(~2,500 tokens).  Rewrite tool descriptions following the grocery "
        f"surface pattern (grocery/tools/surface.py)."
    )


def test_every_tool_has_nonempty_description():
    """A tool with an empty description is a tool an agent cannot select."""
    for tool in _build_tool_list():
        assert tool.get("description"), f"{tool['name']} has no description"


def test_no_tool_description_exceeds_3000_chars():
    """Individual cap: no single description may exceed 3,000 chars.

    The worst offenders today (map_trade_restriction: 2,812; screen_sanctions:
    2,841) are already near this limit.  This test prevents new tools from
    arriving with even longer descriptions.  The target is 550 chars (grocery
    surface pattern); 3,000 is a transitional ceiling.
    """
    oversized = [
        (t["name"], len(t["description"]))
        for t in _build_tool_list()
        if len(t.get("description", "")) > 3000
    ]
    assert not oversized, (
        f"These tools have descriptions over 3,000 chars: {oversized}.  "
        f"Rewrite to be more concise (target: 550 chars per grocery pattern)."
    )
