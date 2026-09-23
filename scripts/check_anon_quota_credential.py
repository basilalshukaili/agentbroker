#!/usr/bin/env python3
"""check_anon_quota_credential.py -- can the configured Supabase credential
actually READ AND WRITE `anon_data_quota`, the table the free-tier anonymous
quota counter (billing/data_quota.py) depends on?

THE INCIDENT THIS EXISTS FOR. See
docs/reviews/2026-09-23-agentbroker-anon-quota-root-cause.md (commit
bb62169). Short version: this service is deployed on the VPS with ONLY
SUPABASE_ANON_KEY (board row 206 item 1 -- no service-role key on a public
box; see ops/vps/deploy_agentbroker_vps.py's NEVER_SHIP_TO_CONTAINER, which
this file does not touch and must not touch). The anon key's raw SELECT on
`anon_data_quota` used to return HTTP 200 with an EMPTY ARRAY -- not an
error -- because RLS had no policy for `anon`, and a raw INSERT's failure
was never checked. Net effect: every anonymous caller looked like a fresh
first call forever, and NOTHING in any log said why, because the failure
never raised and the code that swallowed it logged at debug. If
DATA_METERING_ENABLED had ever been flipped on for this box as-is, every
anonymous caller would have gotten unlimited free premium-data calls,
silently, forever.

billing/data_quota.py's anon-quota path was fixed (2026-09-23) to route
through `anon_data_quota_consume`, a narrow SECURITY DEFINER RPC (see
sql/agentbroker/002_anon_data_quota_security_definer_rpc.sql -- NOT YET
APPLIED, see that file and sql/agentbroker/README.md for the exact apply
command) instead of raw REST calls, and its failures now log loudly and
distinguishably (misconfigured vs outage) instead of identically at debug.
That fix makes the RUNTIME path honest. It does not, by itself, stop
someone from setting DATA_METERING_ENABLED=true on a box where the
migration was never applied, or where the anon role's EXECUTE grant was
since revoked, or where the wrong Supabase project's credentials are
configured -- billing/data_quota.py still fails OPEN in all of those cases,
by design (an infra problem must never block a real caller). THIS SCRIPT is
the pre-flight gate for that: run it and treat anything other than exit 0
as "do not enable metering on this box yet."

    python scripts/check_anon_quota_credential.py --self-test   # offline, no network
    python scripts/check_anon_quota_credential.py               # self-test, then live verdict
    python scripts/check_anon_quota_credential.py --json         # for system_health.py

Exit codes (same 4-state contract as scripts/check_deploy_env.py, one
directory up, for the analogous env/credential-verification job on the same
container):

    0 = self-test passed AND the configured credential can read+write
        anon_data_quota (via anon_data_quota_consume) right now.
    1 = self-test passed but the credential is VERIFIABLY broken: reached
        Supabase and was refused (permission denied, function not deployed
        yet, a parameter/shape mismatch) or returned a response that does
        not match the function's contract. A real, actionable fault.
    2 = SELF-TEST FAILED -- this checker does not trust itself; no live
        verdict was attempted or rendered. UNKNOWN never renders as clean.
    3 = COULD NOT VERIFY AT ALL -- no SUPABASE_URL/key configured in this
        process, or a genuine transport failure (DNS, connection refused,
        timeout, 5xx). Distinct from exit 1 on purpose: this does not mean
        the credential is broken, only that this run could not tell. Not
        clean, but not a proven fault either -- retry once Supabase (or
        this process's env) is reachable.

WHY THIS REUSES billing.data_quota's OWN CLASSIFIER RATHER THAN A SECOND
COPY. `_classify_rpc_exception` and `_verify_response_shape` already encode
exactly "which RPC failures mean misconfigured vs outage" and "what a
trustworthy response looks like" -- the two questions this health check
exists to answer live-fire. A hand-duplicated copy here could drift from
the runtime path's actual behaviour the moment either changed (the "one
source" lesson this workspace has re-learned repeatedly -- see
billing/data_quota.py's own _limits() docstring for the same lesson applied
to quota numbers). Importing them means this check is provably testing the
SAME logic billing/data_quota.py runs in production, not a look-alike.

THE LIVE PROBE never touches a real IP bucket. It calls
anon_data_quota_consume with a fixed, clearly-synthetic bucket key
("healthcheck:check_anon_quota_credential" -- never a valid sha256 hex
digest, so it can never collide with a real IP+date bucket) and a p_limit
of 2**31-1, so the probe call can never itself report "quota exceeded" and
never interferes with real quota accounting. It leaves at most ONE small
row in the table, whose quota_date resets it once per UTC day this check
runs -- harmless, and trivially recognisable/removable by hand if ever
wanted. This is the "actually write" half of the health check: a
SELECT-only probe could not prove the credential can WRITE, which is
exactly the half of this bug that made the old counter permissive.

HARD RULE -- NEVER PRINT, LOG, OR TRANSMIT A SECRET VALUE. This script only
ever reads SUPABASE_URL/SUPABASE_ANON_KEY/SUPABASE_SERVICE_KEY to pass them
to storage.supabase_client.rpc() (which itself never logs them); the
`--json`/text output below prints only booleans, exit codes, HTTP-status-
derived classifications, and the byte LENGTH of environment presence where
relevant -- never a key or URL value.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Optional

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# Probe identity: see module docstring. Never a valid sha256 hex digest (has
# a ":" and non-hex characters), so it can never be mistaken for -- or
# collide with -- a real ip:date bucket_key.
_PROBE_BUCKET_KEY = "healthcheck:check_anon_quota_credential"
_PROBE_LIMIT = 2**31 - 1  # effectively unlimited -- the probe must never itself hit "exceeded"


def _load_local_env_for_manual_run() -> None:
    """For a human running this by hand from a dev machine only. On the VPS
    the container already has SUPABASE_URL/SUPABASE_ANON_KEY injected via
    `docker run --env-file` (ops/vps/deploy_agentbroker_vps.py) before this
    script would ever run there, so this is a no-op in that environment.
    Never overrides an already-set var (production's real env always wins);
    checks the hatchloop-level .env (one directory above agentbroker/, same
    file ops/vps/deploy_agentbroker_vps.py's LOCAL_ENV points at) since
    agentbroker/ has no .env of its own."""
    candidates = [
        os.path.join(_PROJECT_ROOT, ".env"),
        os.path.join(_PROJECT_ROOT, "..", ".env"),
    ]
    for p in candidates:
        p = os.path.abspath(p)
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and v and k not in os.environ:
                    os.environ[k] = v


# ---------------------------------------------------------------------------
# The pure verdict function -- takes an ALREADY-COMPLETED outcome (either a
# successful payload or a caught exception) and turns it into (exit_code,
# status, detail). Kept separate from anything that performs network I/O so
# the self-test below can exercise every branch deterministically, offline.
# ---------------------------------------------------------------------------

def classify_outcome(payload: Optional[dict] = None, exc: Optional[Exception] = None) -> tuple[int, str, str]:
    """Exactly one of `payload` (an RPC response that was returned without
    raising) or `exc` (what rpc() raised) should be given.

    Returns (exit_code, status, detail) per this module's own exit-code
    contract (0 ok / 1 fail / 3 unknown -- 2 is reserved for a self-test
    failure and is never returned from here)."""
    from billing.data_quota import _AnonQuotaRpcFailure, _classify_rpc_exception, _verify_response_shape

    if exc is not None:
        if isinstance(exc, _AnonQuotaRpcFailure):
            return 1, "fail", f"bad_response_shape: {exc}"
        kind, detail = _classify_rpc_exception(exc)
        if kind == "misconfigured":
            return 1, "fail", f"misconfigured: {detail}"
        if kind in ("outage", "unconfigured"):
            return 3, "unknown", f"{kind}: {detail}"
        return 3, "unknown", f"{kind}: {detail}"  # defensive: never invent "ok" or "fail" for an unrecognised kind

    try:
        result = _verify_response_shape(payload)
    except _AnonQuotaRpcFailure as shape_exc:
        return 1, "fail", f"bad_response_shape: {shape_exc}"

    if result["allowed"] is not True:
        # The probe uses a fixed key + an effectively-unlimited p_limit, so
        # this should never legitimately happen. If it does, something about
        # the function's behaviour does not match what this check (and
        # billing/data_quota.py) assume -- a real fault, not "unknown".
        return 1, "fail", (
            f"anon_data_quota_consume returned allowed=False for the probe bucket "
            f"despite p_limit={_PROBE_LIMIT} -- investigate before trusting this "
            f"function's behaviour: {result!r}"
        )
    return 0, "ok", (
        f"anon_data_quota_consume read+wrote successfully "
        f"(remaining={result['remaining']}, count={result['count']})"
    )


# ---------------------------------------------------------------------------
# SELF-TEST -- offline, no network. Proves classify_outcome can render EACH
# of ok / fail / unknown on inputs whose correct answer is known, before any
# live verdict is trusted. Same contract as scripts/check_deploy_env.py and
# scripts/check_install_doc_drift.py's self-tests.
# ---------------------------------------------------------------------------

def _run_self_test() -> tuple[bool, list[str]]:
    lines: list[str] = []
    ok = True

    def check(label: str, payload, exc, want_code: int):
        nonlocal ok
        code, status, detail = classify_outcome(payload=payload, exc=exc)
        passed = code == want_code
        ok = ok and passed
        lines.append(
            f"  {label:<42} -> exit {code} ({status})  (expect {want_code})"
            + ("" if passed else f"  *** MISMATCH: {detail[:120]}")
        )

    # -- exit 0: a well-shaped, allowed=True response -------------------------
    check(
        "good credential, real write",
        {"allowed": True, "remaining": 2147483646, "count": 1}, None, 0,
    )

    # -- exit 1: PROVEN BROKEN, several distinct real-world shapes ------------
    # This is the specific proof the task asked for: demonstrate the checker
    # can FAIL, not just pass, before trusting a live "ok".
    check(
        "permission denied (401)",
        None, RuntimeError("rpc('anon_data_quota_consume') failed: HTTP 401 body=denied"), 1,
    )
    check(
        "permission denied (403)",
        None, RuntimeError("rpc('anon_data_quota_consume') failed: HTTP 403 body=denied"), 1,
    )
    check(
        "function not deployed yet (404/PGRST202)",
        None,
        RuntimeError(
            "rpc('anon_data_quota_consume') failed: HTTP 404 "
            'body={"code":"PGRST202","details":"Searched for the function..."}'
        ),
        1,
    )
    check(
        "malformed response shape (missing fields)",
        {"allowed": True}, None, 1,
    )
    check(
        "malformed response shape (wrong types)",
        {"allowed": "true", "remaining": "1", "count": 1}, None, 1,
    )
    check(
        "well-shaped but allowed=False for an unlimited probe",
        {"allowed": False, "remaining": 0, "count": 999}, None, 1,
    )

    # -- exit 3: genuinely could not tell --------------------------------------
    check(
        "transport error (network unreachable)",
        None, RuntimeError("rpc('anon_data_quota_consume') transport error: timed out"), 3,
    )
    check(
        "Supabase 5xx",
        None, RuntimeError("rpc('anon_data_quota_consume') failed: HTTP 503 body=down"), 3,
    )
    check(
        "no Supabase config at all",
        None,
        RuntimeError(
            "rpc('anon_data_quota_consume') aborted: SUPABASE_URL or service key "
            "not configured. Cannot proceed on spend path without Supabase."
        ),
        3,
    )

    return ok, lines


def self_test(verbose: bool = True) -> int:
    ok, lines = _run_self_test()
    if verbose:
        print("\n".join(lines))
        print("SELF-TEST PASS" if ok else "SELF-TEST FAIL -- this checker cannot be trusted")
    return 0 if ok else 2


# ---------------------------------------------------------------------------
# LIVE PROBE -- one real network round trip, using whatever credential this
# process's environment actually has configured (the exact credential the
# running service would use).
# ---------------------------------------------------------------------------

async def _live_probe() -> tuple[int, str, str]:
    sb_url = os.getenv("SUPABASE_URL", "").rstrip("/")
    svc_key = os.getenv("SUPABASE_SERVICE_KEY", "") or os.getenv("SUPABASE_ANON_KEY", "")
    if not sb_url or not svc_key:
        return 3, "unknown", (
            "SUPABASE_URL / a Supabase key not present in this process's environment "
            "-- cannot verify. (Presence only, never a value: "
            f"SUPABASE_URL={'present' if sb_url else 'absent'}, "
            f"key={'present' if svc_key else 'absent'}.)"
        )

    from billing.data_quota import _today_utc
    from storage.supabase_client import rpc

    try:
        payload = await asyncio.wait_for(
            rpc("anon_data_quota_consume", {
                "p_bucket_key": _PROBE_BUCKET_KEY,
                "p_quota_date": _today_utc(),
                "p_limit": _PROBE_LIMIT,
            }),
            timeout=10.0,
        )
    except Exception as exc:  # noqa: BLE001 -- includes asyncio.TimeoutError
        return classify_outcome(exc=exc)
    return classify_outcome(payload=payload)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--self-test", action="store_true", help="offline only, no network")
    ap.add_argument("--json", action="store_true", help="machine-readable, for system_health.py")
    ap.add_argument("--skip-local-dotenv", action="store_true",
                    help="do not read a local .env for a manual run (production never needs this)")
    a = ap.parse_args(argv)

    def emit(code: int, status: str, detail: str):
        if a.json:
            print(json.dumps({
                "check": "anon_quota_credential", "status": status, "exit": code,
                "detail": detail,
            }))
        else:
            print(detail)
        return code

    if a.self_test:
        return self_test()

    # SELF-TEST BEFORE EVERY VERDICT -- same contract as check_deploy_env.py /
    # check_install_doc_drift.py. A checker that cannot prove it still
    # detects a broken credential must not be trusted to report "ok".
    ok, lines = _run_self_test()
    if not ok:
        return emit(2, "fail", "SELF-TEST FAILED, checker not trusted: "
                    + "; ".join(x.strip() for x in lines))

    if not a.skip_local_dotenv:
        _load_local_env_for_manual_run()

    code, status, detail = asyncio.run(_live_probe())
    if code == 0:
        return emit(0, "ok", detail)
    if code == 1:
        return emit(1, "fail",
                    "anon_data_quota_consume credential check FAILED: " + detail +
                    " | Do not set DATA_METERING_ENABLED=true on this box until this "
                    "passes -- see sql/agentbroker/002_anon_data_quota_security_"
                    "definer_rpc.sql and sql/agentbroker/README.md to apply/verify the "
                    "migration.")
    return emit(3, "unknown", "could not verify anon_data_quota_consume: " + detail)


if __name__ == "__main__":
    sys.exit(main())
