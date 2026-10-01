#!/usr/bin/env python3
"""Does the REAL start-up hook load the durable opt-out list through the anon key, with no failure line?

    python scripts/verify_optout_hydration_live.py [--env-file PATH]

Runs main.lifespan() - the code that logged OPTOUT_HYDRATION_FAILED on every start of the live
container - in this process, against the spine, with the SAME credential the container holds (the anon
JWT: SUPABASE_URL + SUPABASE_ANON_KEY; the service key is deliberately removed from this process's
environment first, so a pass cannot be borrowed from a stronger key). It captures the log output and
checks, in order:

  1. "hydrated N durable opt-outs" is logged;
  2. OPTOUT_HYDRATION_FAILED / OPTOUT_HYDRATION_TRUNCATED / optout_hydration_failed are NOT;
  3. the in-memory consent set now holds N entries (N compared with a count from the owner connection
     when SUPABASE_DB_URL is available - the two must agree).

It READS the list (5 rows today) and prints only counts. It writes nothing. It is the pre-deploy stand-in
for "grep the new container's startup log"; after the deploy, run the same grep on
`docker logs techmate-agentbroker` and expect the identical line.
"""
from __future__ import annotations

import argparse
import asyncio
import io
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# No path is baked in: pass --env-file, or export HATCHLOOP_ENV_FILE (or the variables themselves).
DEFAULT_ENV = os.environ.get("HATCHLOOP_ENV_FILE", "")


def _read_env(path: str, names: list) -> dict:
    out = {}
    p = Path(path)
    if path and p.is_file():
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            k, _, v = line.partition("=")
            if k.strip() in names and v.strip():
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env-file", default=DEFAULT_ENV)
    a = ap.parse_args()

    e = _read_env(a.env_file, ["SUPABASE_URL", "SUPABASE_ANON_KEY", "SUPABASE_DB_URL"])
    if "techmate.om/spine" not in e.get("SUPABASE_URL", "") or not e.get("SUPABASE_ANON_KEY"):
        print("ERROR: SUPABASE_URL must be the spine and SUPABASE_ANON_KEY must be set in the env file")
        return 2
    for k in ("SUPABASE_SERVICE_KEY", "SUPABASE_SERVICE_ROLE_KEY"):
        os.environ.pop(k, None)               # the container never holds these
    os.environ["SUPABASE_URL"] = e["SUPABASE_URL"]
    os.environ["SUPABASE_ANON_KEY"] = e["SUPABASE_ANON_KEY"]

    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)

    import main as app_main
    from compliance import consent_store as cs
    fresh = cs.ConsentStore()
    cs._store = fresh                          # judge THIS boot, not state left by an import

    async def boot():
        async with app_main.lifespan(app_main.app):
            pass
    asyncio.run(boot())
    root.removeHandler(handler)

    text = buf.getvalue()
    hydrated = [ln for ln in text.splitlines() if "hydrated" in ln and "durable opt-outs" in ln]
    bad = [ln for ln in text.splitlines()
           if "OPTOUT_HYDRATION_FAILED" in ln or "OPTOUT_HYDRATION_TRUNCATED" in ln
           or "optout_hydration_failed" in ln]
    held = len(fresh._opted_out)

    expected = None
    if e.get("SUPABASE_DB_URL"):
        try:
            import psycopg2
            c = psycopg2.connect(e["SUPABASE_DB_URL"], connect_timeout=15)
            cur = c.cursor()
            cur.execute("select count(*) from (select distinct recipient_id, channel from public.consent_optouts "
                        "where recipient_id is not null and channel is not null) s")
            expected = cur.fetchone()[0]
            c.close()
        except Exception as exc:  # noqa: BLE001
            print(f"(owner count unavailable: {type(exc).__name__})")

    print("startup log lines about opt-outs:")
    for ln in text.splitlines():
        if "opt-out" in ln.lower() or "optout" in ln.lower():
            print("  " + ln)
    print(f"hydrated line present: {bool(hydrated)}")
    print(f"failure lines: {len(bad)}")
    print(f"entries now in the in-memory consent set: {held}" +
          (f" (distinct pairs on the table: {expected})" if expected is not None else ""))

    ok = bool(hydrated) and not bad and (expected is None or held == expected)
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
