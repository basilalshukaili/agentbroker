"""LIVE proof of the anon-key RPC boundary for `anon_data_quota` (board row
206 follow-up; see sql/agentbroker/002_anon_data_quota_security_definer_rpc.sql
and docs/reviews/2026-09-23-agentbroker-anon-quota-root-cause.md).

Same reasoning as tests/integration/test_operations_rpc_boundary_live.py: a
SQL-side inspection can confirm the GRANTs are *configured* correctly; it
cannot prove what an attacker (or this service's own VPS container) holding
only the anon key can actually do over the network. This makes the real HTTPS
calls that matter, with the SAME key the internet-facing container uses:

  1. A direct `GET /rest/v1/anon_data_quota` with the anon key MUST NOT come
     back with a usable row -- an HTTP 200 with an empty array is graded a
     FAIL here, not a pass, because that is EXACTLY the silent-guard shape
     docs/reviews/2026-09-23-agentbroker-anon-quota-root-cause.md documents
     as the live defect (verified there, read-only, 2026-09-23: anon SELECT
     -> HTTP 200, body `[]`). Only a non-2xx (permission denied) counts as
     the direct door being shut.
  2. The SAME anon key calling `POST /rest/v1/rpc/anon_data_quota_consume`
     MUST succeed (2xx) and return the exact {allowed, remaining, count}
     shape the function contracts to return -- the narrow door must actually
     open, and scripts/check_anon_quota_credential.py's classify_outcome
     must call it "ok".
  3. A garbage/deliberately-broken credential calling the SAME RPC MUST be
     refused (this is the "prove it can fail" requirement made real, not
     mocked): scripts/check_anon_quota_credential.py's classify_outcome must
     call it "fail", not "ok" and not "unknown".

Uses a fixed, clearly-synthetic bucket key ("boundary-probe-...", never a
valid sha256 hex digest) so it can never collide with or consume a real IP's
quota, and a p_limit high enough that the probe call itself can never report
"exceeded".

SKIPS ITSELF (does not fail) when SUPABASE_URL / SUPABASE_ANON_KEY are not in
the environment -- this is a live check, run manually after applying
sql/agentbroker/002_anon_data_quota_security_definer_rpc.sql (see
sql/agentbroker/README.md for the exact command and for how to load these two
vars into this process's environment without ever printing them).

HARD RULE: this file never prints, logs, or asserts on a KEY value itself --
only HTTP status codes and response SHAPE (row present / absent / error).
"""
from __future__ import annotations

import os
import uuid

import pytest

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")

pytestmark = pytest.mark.skipif(
    not SUPABASE_URL or not SUPABASE_ANON_KEY,
    reason="SUPABASE_URL/SUPABASE_ANON_KEY not in the environment -- this is a "
           "live check, run manually after applying "
           "sql/agentbroker/002_anon_data_quota_security_definer_rpc.sql (see "
           "sql/agentbroker/README.md for the exact command).",
)

_PROBE_LIMIT = 2**31 - 1  # effectively unlimited -- the probe must never itself hit "exceeded"


def _anon_headers() -> dict:
    return {"apikey": SUPABASE_ANON_KEY, "Authorization": f"Bearer {SUPABASE_ANON_KEY}"}


def test_direct_table_access_is_not_a_usable_door():
    """The anon key must NOT be able to read `anon_data_quota` via the raw
    PostgREST table endpoint -- not because it returns wrong data, but
    because it must not be a usable path AT ALL. A 200 with `[]` is a FAIL:
    this is the exact live defect this migration exists to close."""
    import httpx

    with httpx.Client(timeout=10.0) as client:
        resp = client.get(
            f"{SUPABASE_URL}/rest/v1/anon_data_quota",
            headers=_anon_headers(),
            params={"limit": 1},
        )

    assert resp.status_code not in (200, 201), (
        f"direct anon-key table access to `anon_data_quota` returned HTTP "
        f"{resp.status_code} -- expected a permission error (401/403/404). "
        f"A 2xx here (even with an empty body) means the anon role still has "
        f"SOME grant on this table; see "
        f"sql/agentbroker/002_anon_data_quota_security_definer_rpc.sql section 1 "
        f"(REVOKE) and re-apply/verify it."
    )


def test_rpc_door_is_open_for_the_same_key_and_shape_is_honest():
    """The SAME anon key, through the narrow RPC, must actually work -- and
    the response must have the exact shape billing/data_quota.py's
    _verify_response_shape (and scripts/check_anon_quota_credential.py's
    classify_outcome) require before trusting `allowed`."""
    import httpx

    probe_bucket = f"boundary-probe-{uuid.uuid4().hex}"  # never a valid sha256 hex digest
    with httpx.Client(timeout=10.0) as client:
        resp = client.post(
            f"{SUPABASE_URL}/rest/v1/rpc/anon_data_quota_consume",
            headers={**_anon_headers(), "Content-Type": "application/json"},
            json={
                "p_bucket_key": probe_bucket,
                "p_quota_date": "2000-01-01",
                "p_limit": _PROBE_LIMIT,
            },
        )

    assert resp.status_code in (200, 201), (
        f"anon_data_quota_consume RPC call with the anon key returned HTTP "
        f"{resp.status_code} (expected 2xx) -- check the function exists, is "
        f"SECURITY DEFINER, owned by a BYPASSRLS role, and anon has EXECUTE "
        f"(see the verification queries at the bottom of "
        f"sql/agentbroker/002_anon_data_quota_security_definer_rpc.sql)."
    )
    body = resp.json()
    assert isinstance(body, dict), f"expected a JSON object, got {body!r}"
    assert body.get("allowed") is True, f"a fresh probe bucket must be allowed: {body!r}"
    assert isinstance(body.get("remaining"), int)
    assert isinstance(body.get("count"), int)

    from billing.data_quota import _verify_response_shape
    _verify_response_shape(body)  # must not raise

    from scripts.check_anon_quota_credential import classify_outcome
    code, status, _detail = classify_outcome(payload=body)
    assert (code, status) == (0, "ok")


def test_a_deliberately_broken_credential_is_correctly_reported_as_fail():
    """THE 'PROVE IT CAN FAIL' REQUIREMENT, made real rather than mocked: a
    garbage anon key against the SAME endpoint must be refused, and
    scripts/check_anon_quota_credential.py's classify_outcome must grade
    that refusal 'fail' (exit 1), never 'ok' and never 'unknown'. Never uses
    a real credential with one character flipped (that would still be a
    plausible-looking secret); uses an obviously-synthetic string instead."""
    import httpx

    garbage_key = "not-a-real-key-" + uuid.uuid4().hex
    with httpx.Client(timeout=10.0) as client:
        resp = client.post(
            f"{SUPABASE_URL}/rest/v1/rpc/anon_data_quota_consume",
            headers={
                "apikey": garbage_key,
                "Authorization": f"Bearer {garbage_key}",
                "Content-Type": "application/json",
            },
            json={
                "p_bucket_key": f"boundary-probe-{uuid.uuid4().hex}",
                "p_quota_date": "2000-01-01",
                "p_limit": _PROBE_LIMIT,
            },
        )

    assert resp.status_code not in (200, 201), (
        "a garbage credential must be refused by Supabase (expected 401/403/etc); "
        f"got HTTP {resp.status_code} -- if this is 2xx, PostgREST is not actually "
        "checking the apikey/Authorization header, which is a much bigger problem "
        "than this migration."
    )

    from billing.data_quota import _classify_rpc_exception
    from scripts.check_anon_quota_credential import classify_outcome

    synthetic_exc = RuntimeError(
        f"rpc('anon_data_quota_consume') failed: HTTP {resp.status_code} "
        f"body={resp.text[:200]!r}"
    )
    kind, _ = _classify_rpc_exception(synthetic_exc)
    code, status, _detail = classify_outcome(exc=synthetic_exc)
    assert kind == "misconfigured", (
        f"a garbage credential's real HTTP {resp.status_code} response classified as "
        f"{kind!r}, not 'misconfigured' -- update _classify_rpc_exception's status-code "
        f"buckets in billing/data_quota.py to cover this code."
    )
    assert (code, status) == (1, "fail")
