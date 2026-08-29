"""
Every worked example must be a call that would actually succeed.

WHY THIS IS NOT PEDANTRY. The `user_query_examples` block is rendered into each
tool's description, which is the few-shot an LLM copies when it decides how to
call us. A wrong argument name there does not produce a slightly-off call - it
produces a confidently malformed one, on the agent's FIRST attempt, at the
moment it is deciding whether this server works.

Four of them were wrong, found by a buyer evaluating the live product:

    send_message                     used recipient_id, message,
                                     channel_preference, country_code
                                     needs recipient, message_type, content
    send_transactional_confirmation  used recipient_id, channel_preference
                                     needs recipient, data
    handle_inbound                   used channel
                                     needs inbound_channel, smb_id
    escalate_to_human                used summary
                                     needs context

Every key in the send_message example was wrong. And these are precisely the
PAID tools - the free ones were all correct - so the failure landed on the
first billable call, which is the worst possible place for it.

The manifest's own `examples` field was right the whole time. Only the
few-shots drifted, because nothing executed them. This does.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

with open(os.path.join(ROOT, "manifest", "manifest.json"), encoding="utf-8") as fh:
    OPERATIONS = json.load(fh)["operations"]


def _cases():
    out = []
    for op in OPERATIONS:
        schema = op.get("input_schema") or {}
        for i, ex in enumerate(op.get("user_query_examples") or []):
            args = ((ex.get("agent_call") or {}).get("arguments"))
            if isinstance(args, dict):
                out.append((f"{op['name']}[{i}]", op["name"], schema, args))
        for i, ex in enumerate(op.get("examples") or []):
            if isinstance(ex.get("input"), dict):
                out.append((f"{op['name']}.examples[{i}]", op["name"], schema,
                            ex["input"]))
    return out


CASES = _cases()


def test_there_are_examples_to_check():
    """Guard the guard - an empty list passes everything below."""
    assert len(CASES) >= 20, (
        f"only collected {len(CASES)} example(s); the manifest shape probably "
        f"changed and this file is checking almost nothing")


@pytest.mark.parametrize("label,tool,schema,args",
                         CASES, ids=[c[0] for c in CASES])
def test_example_arguments_match_the_schema(label, tool, schema, args):
    props = set((schema.get("properties") or {}).keys())
    required = set(schema.get("required") or [])
    used = set(args.keys())

    unknown = sorted(used - props)
    assert not unknown, (
        f"{label} passes {unknown}, which this tool does not accept. An agent "
        f"copying this few-shot sends a malformed call on its first attempt. "
        f"Valid top-level fields: {sorted(props)}")

    missing = sorted(required - used)
    assert not missing, (
        f"{label} omits required field(s) {missing}. The call it demonstrates "
        f"cannot succeed. Required: {sorted(required)}")


@pytest.mark.parametrize("label,tool,schema,args",
                         CASES, ids=[c[0] for c in CASES])
def test_example_enum_values_are_valid(label, tool, schema, args):
    """A plausible-but-wrong enum value is the other way a few-shot misleads.

    'agent_blocked' reads exactly like a real reason code. It is not one, and
    an agent that copies it gets a typed rejection it cannot diagnose from the
    description that taught it the value.
    """
    props = schema.get("properties") or {}
    for key, value in args.items():
        allowed = (props.get(key) or {}).get("enum")
        if allowed and isinstance(value, str):
            assert value in allowed, (
                f"{label} uses {key}={value!r}, which is not one of {allowed}")
