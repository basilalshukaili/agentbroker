"""LIVE proof that the find_business free-trial counter works through the anon key.

RUN THIS BEFORE A BUILD CONTAINING billing/anon_trial.py GOES LIVE. The trial
fails CLOSED: if `anon_trial_reserve` / `anon_trial_release` (sql/agentbroker/
008_anon_trial_reserve_release_rpc.sql, which lives in the hatchloop workspace,
outside this repo) are not applied, every keyless find_business call answers
"get a key". That is the intended safe direction and also a self-inflicted
outage of the keyless path, and nothing in the offline suite can tell the
difference between "applied" and "not applied".

Same reasoning as tests/integration/test_anon_data_quota_credential_live.py: a
SQL-side check proves the GRANTs are configured; only the real HTTPS call with
the SAME anon key the container holds proves the narrow door opens and behaves.

  1. reserve on a fresh probe caller is allowed, in exactly the shape
     billing.anon_trial._verify_reserve_shape requires before it will trust
     `allowed`.
  2. a second reserve past a limit of 1 is REFUSED with reason caller_limit and
     consumes NOTHING (neither counter moves) - the property that stops one
     over-limit caller from draining the global ceiling.
  3. release gives the slot back, so the caller can reserve again; a second
     release finds nothing to give back and says so (never below zero).
  4. malformed input is rejected loudly (HTTP 400), not turned into a bucket.
  5. a garbage credential is refused by the same endpoint (the check can fail).
  6. the direct table door on `anon_data_quota` is STILL shut - this migration
     must not have changed that (a 2xx, even an empty one, is a FAIL).

It uses a dedicated probe tool name ("boundary_probe") and a random 64-hex
caller key, so it can never touch a real caller's allowance or the real
`find_business` global counter. It leaves the probe rows at count 0.

SKIPS ITSELF when SUPABASE_URL / SUPABASE_ANON_KEY are not in the environment.
HARD RULE: never prints or asserts on a key value - only status codes and shapes.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")

pytestmark = pytest.mark.skipif(
    not SUPABASE_URL or not SUPABASE_ANON_KEY,
    reason="SUPABASE_URL/SUPABASE_ANON_KEY not in the environment -- this is a "
           "live check, run manually after applying "
           "sql/agentbroker/008_anon_trial_reserve_release_rpc.sql.",
)

_TOOL = "boundary_probe"


def _headers(key: str | None = None) -> dict:
    k = key or SUPABASE_ANON_KEY
    return {"apikey": k, "Authorization": f"Bearer {k}", "Content-Type": "application/json"}


def _post(fn: str, payload: dict, key: str | None = None):
    import httpx
    with httpx.Client(timeout=10.0) as client:
        return client.post(f"{SUPABASE_URL}/rest/v1/rpc/{fn}",
                           headers=_headers(key), json=payload)


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _reserve(caller: str, caller_limit: int = 1, global_limit: int = 2**31 - 1):
    return _post("anon_trial_reserve", {
        "p_tool": _TOOL, "p_caller_key": caller, "p_day": _today(),
        "p_caller_limit": caller_limit, "p_global_limit": global_limit})


def _release(caller: str):
    return _post("anon_trial_release", {
        "p_tool": _TOOL, "p_caller_key": caller, "p_day": _today()})


def test_reserve_refuse_release_round_trip_through_the_anon_key():
    from billing.anon_trial import _verify_reserve_shape

    caller = uuid.uuid4().hex + uuid.uuid4().hex          # 64 hex chars

    r1 = _reserve(caller)
    assert r1.status_code in (200, 201), (
        f"anon_trial_reserve with the anon key returned HTTP {r1.status_code}. "
        f"404/PGRST202 means 008 is not applied; 401/403 means anon lacks "
        f"EXECUTE. Until this passes, deploying the trial makes every keyless "
        f"find_business call answer 'get a key'.")
    first = _verify_reserve_shape(r1.json())
    assert first["allowed"] is True and first["reason"] == "ok"
    assert first["caller_count"] == 1
    global_after_first = first["global_count"]

    # Past the caller's limit of 1: refused, and NOTHING moves.
    r2 = _reserve(caller)
    assert r2.status_code in (200, 201)
    second = _verify_reserve_shape(r2.json())
    assert second["allowed"] is False and second["reason"] == "caller_limit"
    assert second["caller_count"] == 1, "a refused reservation consumed a slot"
    assert second["global_count"] == global_after_first, (
        "a refused reservation moved the GLOBAL counter - an over-limit caller "
        "could then drain the daily ceiling for everybody")

    # Release gives the slot back...
    rel = _release(caller)
    assert rel.status_code in (200, 201)
    assert rel.json() == {"released": True}
    r3 = _reserve(caller)
    assert _verify_reserve_shape(r3.json())["allowed"] is True

    # ...and leaves the probe rows at zero, never below.
    assert _release(caller).json() == {"released": True}
    assert _release(caller).json() == {"released": False}


def test_a_global_ceiling_of_zero_refuses_with_global_limit_and_consumes_nothing():
    from billing.anon_trial import _verify_reserve_shape
    caller = uuid.uuid4().hex + uuid.uuid4().hex
    out = _verify_reserve_shape(_reserve(caller, caller_limit=5, global_limit=0).json())
    assert out["allowed"] is False and out["reason"] == "global_limit"
    assert out["caller_count"] == 0, "a global refusal burned the caller's own allowance"


@pytest.mark.parametrize("payload", [
    {"p_tool": "Bad-Tool", "p_caller_key": "a" * 64, "p_day": "2026-09-30",
     "p_caller_limit": 1, "p_global_limit": 1},
    {"p_tool": _TOOL, "p_caller_key": "not-hex", "p_day": "2026-09-30",
     "p_caller_limit": 1, "p_global_limit": 1},
    {"p_tool": _TOOL, "p_caller_key": "a" * 64, "p_day": "yesterday",
     "p_caller_limit": 1, "p_global_limit": 1},
    {"p_tool": _TOOL, "p_caller_key": "a" * 64, "p_day": "2026-09-30",
     "p_caller_limit": -1, "p_global_limit": 1},
])
def test_malformed_input_is_rejected_loudly_not_turned_into_a_bucket(payload):
    r = _post("anon_trial_reserve", payload)
    assert r.status_code == 400, (
        f"malformed input got HTTP {r.status_code}; the function must raise "
        f"(errcode 22023) rather than invent a counter for it")


def test_a_garbage_credential_is_refused_by_the_same_endpoint():
    r = _post("anon_trial_reserve", {
        "p_tool": _TOOL, "p_caller_key": "b" * 64, "p_day": _today(),
        "p_caller_limit": 1, "p_global_limit": 1},
        key="not-a-real-key-" + uuid.uuid4().hex)
    assert r.status_code not in (200, 201), (
        "a garbage credential was accepted - this check can no longer fail")


def test_the_direct_table_door_is_still_shut():
    """008 reuses anon_data_quota and must not have opened it."""
    import httpx
    with httpx.Client(timeout=10.0) as client:
        r = client.get(f"{SUPABASE_URL}/rest/v1/anon_data_quota",
                       headers=_headers(), params={"limit": 1})
    assert r.status_code not in (200, 201), (
        f"anon-key access to `anon_data_quota` returned HTTP {r.status_code}; "
        f"expected a permission error. A 2xx (even []) means the anon role has "
        f"a grant on the table - see sql/agentbroker/002 section 1.")
