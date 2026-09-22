"""LIVE proof of the anon-key RPC boundary (board row 206, item 1).

A SQL-side inspection (see the queries at the bottom of
sql/agentbroker/001_operations_security_definer_rpc.sql) can confirm the
GRANTs are *configured* correctly. It cannot prove what an attacker holding
only the anon key can actually do over the network -- and "the guard looks
right in the migration file" is exactly the kind of confidence the
`postgres-guards-that-do-not-guard` lesson (FORCE RLS silently running views
as OWNER) says not to trust unverified. This test makes the two real HTTPS
calls that matter, with the SAME key the internet-facing container uses:

  1. A direct `GET /rest/v1/operations` with the anon key MUST NOT come back
     with a row -- an HTTP 200 with an empty array is graded a FAIL here,
     not a pass, because that is indistinguishable from "the store has
     nothing" and is the exact silent-guard shape this board row exists to
     close (see storage/outcome_store.py's OutcomeStoreUnavailable
     docstring). Only a non-2xx (permission denied) counts as the door being
     shut.
  2. The SAME anon key calling `POST /rest/v1/rpc/operations_get_by_id` MUST
     succeed (2xx) -- the narrow door must actually open.

SKIPS ITSELF (does not fail) when SUPABASE_URL / SUPABASE_ANON_KEY are not
in the environment -- this repo's tests run unconfigured by convention (see
any of the unit tests' "this process has no SUPABASE_URL/key configured"
comments), and CI has no reason to hold these. Run it manually after
applying the migration (sql/agentbroker/README.md has the exact command):
these two vars must be present in THIS process's environment (never as a
command-line argument, never printed) -- e.g.

    python -c "
    import os, subprocess
    env = dict(os.environ)
    with open(r'C:\\TechMate\\projects\\hatchloop\\.env', encoding='utf-8') as fh:
        for line in fh:
            line = line.strip()
            if line.startswith(('SUPABASE_URL=', 'SUPABASE_ANON_KEY=')):
                k, _, v = line.partition('=')
                env[k] = v.strip().strip('\"').strip(\"'\")
    subprocess.run(['python', '-m', 'pytest',
                    'tests/integration/test_operations_rpc_boundary_live.py', '-v'],
                   env=env, cwd=r'C:\\TechMate\\projects\\hatchloop\\agentbroker')
    "

HARD RULE: this file never prints, logs, or asserts on the KEY value itself
-- only HTTP status codes and response SHAPE (row present / absent / error).
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
           "sql/agentbroker/001_operations_security_definer_rpc.sql (see "
           "sql/agentbroker/README.md for the exact command).",
)


def _anon_headers() -> dict:
    return {"apikey": SUPABASE_ANON_KEY, "Authorization": f"Bearer {SUPABASE_ANON_KEY}"}


def test_direct_table_access_is_not_a_usable_door():
    """The anon key must NOT be able to read `operations` via the raw
    PostgREST table endpoint -- not because it returns wrong data, but
    because it must not be a usable path AT ALL. A 200 with `[]` is a FAIL:
    indistinguishable from "no such row", which is the exact defect this
    board row exists to close one layer up (OutcomeStoreUnavailable)."""
    import httpx

    probe_id = f"boundary-probe-{uuid.uuid4().hex}"  # never a real operation_id
    with httpx.Client(timeout=10.0) as client:
        resp = client.get(
            f"{SUPABASE_URL}/rest/v1/operations",
            headers=_anon_headers(),
            params={"operation_id": f"eq.{probe_id}", "limit": 1},
        )

    assert resp.status_code not in (200, 201), (
        f"direct anon-key table access to `operations` returned HTTP "
        f"{resp.status_code} -- expected a permission error (401/403/404). "
        f"A 2xx here means the anon role still has SOME grant on this "
        f"table; see sql/agentbroker/001_operations_security_definer_rpc.sql "
        f"section 1 (REVOKE) and re-apply/verify it."
    )


def test_rpc_door_is_open_for_the_same_key():
    """The SAME anon key, through the narrow RPC, must actually work --
    proving the boundary closes the table without also closing the one
    door this service needs."""
    import httpx

    probe_id = f"boundary-probe-{uuid.uuid4().hex}"  # deliberately unknown
    with httpx.Client(timeout=10.0) as client:
        resp = client.post(
            f"{SUPABASE_URL}/rest/v1/rpc/operations_get_by_id",
            headers={**_anon_headers(), "Content-Type": "application/json"},
            json={"p_operation_id": probe_id},
        )

    assert resp.status_code in (200, 201), (
        f"operations_get_by_id RPC call with the anon key returned HTTP "
        f"{resp.status_code} (expected 2xx) -- check the function exists, "
        f"is SECURITY DEFINER, owned by a BYPASSRLS role, and anon has "
        f"EXECUTE (see the verification queries at the bottom of "
        f"sql/agentbroker/001_operations_security_definer_rpc.sql)."
    )
    # A genuinely unknown operation_id must resolve to SQL NULL -> JSON null,
    # never an error and never a fabricated row.
    assert resp.json() is None, (
        "operations_get_by_id returned a non-null body for a fabricated, "
        "never-written operation_id -- investigate before trusting this "
        "RPC's genuine-miss behaviour."
    )
