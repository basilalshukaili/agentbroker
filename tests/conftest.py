"""
Suite-wide fixtures.

THE ONE THING IN HERE, and why it is autouse.

find_business has a free-trial gate (billing/anon_trial.py): a keyless caller
gets a few successful calls, counted in Supabase, and the gate FAILS CLOSED when
it cannot reach the counter. This suite runs with no Supabase at all, so without
help every test that calls find_business without a key - dozens of them, none of
which is about the trial - would be refused with "get a key".

So the counter's transport is replaced, for every test, by one that admits
everything and remembers nothing. That keeps those tests about what they are
about. It is NOT how the gate is tested: tests/unit/test_find_business_free_trial.py
installs a faithful model of the database functions (tests/anon_trial_fake.py)
over this one and exercises the real limits, the real refusal and the real
fail-closed path.

A test that needs the gate closed for some other reason should install its own
transport the same way; nothing else in the suite should ever depend on the
permissive one.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _find_business_trial_counter_admits_everything(monkeypatch):
    import billing.anon_trial as anon_trial

    async def _admit_everything(fn: str, payload: dict):
        if fn == "anon_trial_reserve":
            return {"allowed": True, "reason": "ok",
                    "caller_count": 1, "global_count": 1}
        if fn == "anon_trial_release":
            return {"released": True}
        raise AssertionError(f"unexpected trial rpc {fn!r} in the default fixture")

    monkeypatch.setattr(anon_trial, "_rpc", _admit_everything)
