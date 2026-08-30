#!/usr/bin/env python3
"""Load the EU and UK sanctions lists into the database.

WHY THE DATABASE AND NOT MEMORY. These lists were first held in each worker
process: ~172MB of parsed index plus ~72MB of cached source text. On our
instance that was an out-of-memory kill - the origin entered a restart loop and
served 502 to everything. Two compaction attempts made it worse or barely
better, because Python object overhead dominates and 46,000 records are simply
not cheap enough to live in every process.

Postgres does not care about 46,000 rows. The data survives restarts, does not
multiply by worker count, and a screen becomes an indexed lookup rather than a
linear scan over a quarter of a gigabyte.

WHAT IS STORED, and why both columns exist:
  * `name_key` - normalised tokens, sorted and joined. The screening rule only
    ASSERTS a match on identical token sets, so this is an exact-equality
    lookup and it is the only thing a finding can rest on.
  * `tokens` - the same tokens as an array with a GIN index, for the CANDIDATE
    half. Near-misses are surfaced as `possible_matches_unverified` rather than
    dropped, and that needs overlap, not equality.

THE UK FEED SETS A TRAP: the older OFSI Consolidated List was closed in January
2026 and its endpoints can still answer with stale data instead of an error - a
silently outdated sanctions screen, which is the worst failure this tool has.
We use the current FCDO-published list.

Usage:
    python scripts/refresh_sanctions_lists.py            # refresh both
    python scripts/refresh_sanctions_lists.py --check    # report counts only
"""
from __future__ import annotations

import csv
import io
import json
import os
import subprocess
import tempfile
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
ROOT = os.path.dirname(REPO)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from core.screen_sanctions import (  # noqa: E402
    _EU_CSV_URL, _UK_CSV_URL, _eu_parse, _uk_parse, _normalize_name,
)

BATCH = 500


def _env() -> dict:
    out = {}
    with open(os.path.join(ROOT, ".env"), encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


class ShortRead(Exception):
    """The download ended early. NEVER treat this as a list."""


def _fetch(url: str) -> str:
    """Download a list file, and PROVE it arrived whole.

    THIS FUNCTION SHIPPED BROKEN AND I CAUGHT IT WITHIN THE HOUR. It ran curl,
    decoded stdout, and ignored both the exit code and stderr. Two downloads
    seconds apart returned 25.2MB and 19.9MB - the EU feed had not changed,
    the transfer was being cut short - and curl said so on stderr, into the
    void.

    A truncated sanctions list is the most dangerous failure this product has.
    It does not error. It loads, it looks healthy, the row count is large, and
    the tool answers "no match" with full confidence about a name that was in
    the part that never arrived.

    THEN THE FIX ITSELF WAS WRONG, in a way worth keeping written down. It
    asked for the headers in a SEPARATE request and compared that
    Content-Length against a second, later download. The EU endpoint is
    genuinely unstable - it has served 25.2MB, 19.9MB and a timeout within
    minutes - so the two requests could legitimately disagree, and the check
    then reported "TRUNCATED, -19169024 missing" for a body that was LARGER
    than declared. A guard whose own failure mode is a false alarm gets
    switched off, and it also doubled the bytes we pull from a public feed.

    One request now. The headers and the body come from the same response, so
    they cannot describe different downloads.
    """
    fd, hdr_path = tempfile.mkstemp(suffix=".hdr")
    os.close(fd)
    try:
        p = subprocess.run(
            ["curl", "-sS", "-L", "--fail", "--retry", "3",
             "--retry-all-errors", "-D", hdr_path, "--max-time", "300", url],
            capture_output=True, timeout=360)
        got = len(p.stdout)

        if p.returncode != 0:
            raise ShortRead(
                f"curl exit {p.returncode}: "
                f"{(p.stderr or b'').decode('utf-8', 'replace').strip()[:200]}")

        # Content-Length from THIS response. A redirect chain writes several
        # header blocks into the file, so the LAST one is the one that
        # described the body we actually received.
        expect = None
        with open(hdr_path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.lower().startswith("content-length:"):
                    try:
                        expect = int(line.split(":", 1)[1].strip())
                    except ValueError:
                        pass

        if expect is not None and got < expect:
            raise ShortRead(f"got {got} bytes, server declared {expect} "
                            f"({expect - got} missing) - TRUNCATED")
        if got < 1_000_000:
            raise ShortRead(
                f"only {got} bytes; a sanctions list is never this small")

        if expect is None:
            note = "no length header"
        elif got == expect:
            note = "matches Content-Length"
        else:
            # Larger than declared is not truncation. It happens with chunked
            # or compressed responses; say what we saw rather than guess.
            note = f"Content-Length said {expect/1e6:.1f} MB, body was larger"
        print(f"  downloaded {got/1e6:.1f} MB ({note})")
        return p.stdout.decode("utf-8-sig", errors="replace")
    finally:
        try:
            os.unlink(hdr_path)
        except OSError:
            pass


def _rows_for(records: list[dict], list_code: str, stamp: str) -> list[dict]:
    """Records -> database rows, deduped on (list_code, name_key).

    A duplicate key inside one batch makes Postgres reject the WHOLE upsert
    with "cannot affect row a second time", so the dedupe is not tidiness - it
    is the difference between loading and not loading.
    """
    seen: set[str] = set()
    out = []
    for rec in records:
        toks = _normalize_name(rec["name"]).split()
        if not toks:
            continue
        key = " ".join(sorted(toks))
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "list_code": list_code,
            "name_key": key,
            "tokens": toks,
            "display_name": rec["name"][:200],
            "entity_id": rec["entity_id"][:64],
            "programme": (rec.get("programme") or "")[:120] or None,
            "etype": rec.get("etype") or "ENTITY",
            # SENT EXPLICITLY, not left to the column default. PostgREST's
            # merge-duplicates updates only the columns in the payload, so a
            # row that already existed would keep its ORIGINAL refreshed_at
            # for ever - and the whole staleness system downstream reads that
            # column. The freshness check would have frozen at the first load
            # and reported a year-old index as current.
            "refreshed_at": stamp,
            # A HINT, never a filter - screen_sanctions annotates matches with
            # it and ranks on it, but must never drop a match for a country
            # mismatch. Our country data is address/nationality, not an
            # exhaustive record of where a sanctioned party operates, so
            # excluding on it would manufacture false negatives.
            "countries": rec.get("countries") or None,
        })
    return out


def _upsert(env: dict, rows: list[dict]) -> int:
    url = env["SUPABASE_URL"].rstrip("/") + "/rest/v1/sanctions_names?on_conflict=list_code,name_key"
    key = env["SUPABASE_SERVICE_KEY"]
    done = 0
    for i in range(0, len(rows), BATCH):
        chunk = rows[i:i + BATCH]
        # THE BODY GOES IN A FILE, NOT ON THE COMMAND LINE. A 500-row batch of
        # JSON is well past the Windows argv limit and the first attempt died
        # with "The filename or extension is too long" - the same trap that had
        # been silently truncating prompts sent to the node agent.
        import tempfile
        fd, tmp = tempfile.mkstemp(suffix=".json", text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(chunk, fh)
            p = subprocess.run(
                ["curl", "-sS", "--max-time", "120", "-X", "POST", url,
                 "-H", f"apikey: {key}", "-H", f"Authorization: Bearer {key}",
                 "-H", "Content-Type: application/json",
                 "-H", "Prefer: resolution=merge-duplicates,return=minimal",
                 "--data-binary", "@" + tmp],
                capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=180)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        # CHECK THE EXIT CODE, NOT JUST THE BODY.
        #
        # With `Prefer: return=minimal` a SUCCESSFUL PostgREST write returns an
        # empty body. A curl failure - DNS, timeout, connection refused - also
        # produces empty stdout. Identical. So a total network outage made this
        # function report every batch as written, and the caller printed
        # "upserted 23941" having written nothing.
        #
        # That is the same defect as the truncated download two functions up,
        # in the file where I fixed it. Found by an adversarial review of this
        # morning's work, which is the only reason it is not still here.
        if p.returncode != 0:
            print(f"  batch {i//BATCH + 1} FAILED: curl exit {p.returncode}: "
                  f"{(p.stderr or '').strip()[:180]}")
            return done
        body = (p.stdout or "").strip()
        if body and not body.startswith("["):
            print(f"  batch {i//BATCH + 1} FAILED: {body[:200]}")
            return done
        done += len(chunk)
        if (i // BATCH) % 10 == 0:
            print(f"    {done}/{len(rows)}")
    return done


def _count(env: dict, list_code: str) -> str:
    url = (env["SUPABASE_URL"].rstrip("/")
           + f"/rest/v1/sanctions_names?select=name_key&list_code=eq.{list_code}")
    key = env["SUPABASE_SERVICE_KEY"]
    p = subprocess.run(["curl", "-sS", "-I", "--max-time", "60", url,
                        "-H", f"apikey: {key}", "-H", f"Authorization: Bearer {key}",
                        "-H", "Prefer: count=exact", "-H", "Range: 0-0"],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=90)
    if p.returncode != 0:
        return f"UNKNOWN (count query failed: curl exit {p.returncode})"
    for line in (p.stdout or "").splitlines():
        if line.lower().startswith("content-range"):
            return line.split("/")[-1].strip()
    return "UNKNOWN (no content-range header)"


def _count_at(env: dict, list_code: str, stamp: str) -> int:
    """How many rows carry EXACTLY this run's stamp. The sweep's evidence."""
    from urllib.parse import quote
    url = (env["SUPABASE_URL"].rstrip("/")
           + f"/rest/v1/sanctions_names?select=name_key&list_code=eq.{list_code}"
           + f"&refreshed_at=eq.{quote(stamp, safe='')}")
    key = env["SUPABASE_SERVICE_KEY"]
    p = subprocess.run(["curl", "-sS", "-I", "--max-time", "60", url,
                        "-H", f"apikey: {key}", "-H", f"Authorization: Bearer {key}",
                        "-H", "Prefer: count=exact", "-H", "Range: 0-0"],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=90)
    for line in (p.stdout or "").splitlines():
        if line.lower().startswith("content-range"):
            tail = line.split("/")[-1].strip()
            return int(tail) if tail.isdigit() else 0
    return 0


def _sweep_delisted(env: dict, list_code: str, stamp: str, kept: int) -> int:
    """Remove rows this refresh did NOT see - i.e. entries that were delisted.

    An upsert only ever adds and updates. Without this, a person removed from
    the EU list stays in our index for ever and keeps matching, and we tell a
    customer their counterparty is sanctioned when they are not. On a
    compliance tool that is not a stale-data annoyance; it is a false
    accusation about a named individual, and it is the mirror image of the
    truncated-download bug: one invents matches, the other hides them.

    THE GUARD MATTERS AS MUCH AS THE SWEEP. A delete keyed on "not seen this
    run" is exactly as destructive as the run was incomplete. The download now
    proves it arrived whole, but this is a second, independent line: if this
    refresh carries less than 70% of what is already indexed, something is
    wrong with the feed and we keep the older data instead of deleting most of
    a sanctions list on the strength of one bad fetch.
    """
    # VERIFY AGAINST THE DATABASE, NOT AGAINST THE CALLER'S NUMBER.
    #
    # The guard below used to trust `kept` - the count the caller said it
    # wrote. Testing this function with a stamp that did not match what was
    # actually in the table deleted the ENTIRE EU list, 23,941 rows, while the
    # guard read as satisfied. A safety check fed by the same assumption it is
    # meant to protect against is not a safety check.
    #
    # So: count what is REALLY stamped with this run first. If the database
    # does not agree that this run wrote a full list, nothing gets deleted.
    fresh = _count_at(env, list_code, stamp)
    if fresh < kept:
        print(f"  {list_code}: NOT sweeping - the database holds {fresh} row(s) "
              f"stamped for this run but the loader reported {kept}. Those must "
              f"agree before anything is deleted.")
        return 0

    have = _count(env, list_code)
    prior = int(have) if str(have).isdigit() else 0
    if prior and kept < prior * 0.7:
        print(f"  {list_code}: REFUSING to sweep - this run has {kept} name(s) "
              f"against {prior} indexed. Too big a drop to trust; keeping the "
              f"existing rows. Investigate the feed.")
        return 0

    # THE "+" IN "+00:00" MUST BE PERCENT-ENCODED. In a URL query string a raw
    # "+" means a space, so `refreshed_at=lt.2026-08-30T12:00:00+00:00` reached
    # PostgREST as a malformed timestamp, matched nothing, and deleted nothing -
    # while the function returned 0 and looked like "no delistings today".
    # A silent no-op is the whole bug class this file keeps running into.
    from urllib.parse import quote
    url = (env["SUPABASE_URL"].rstrip("/")
           + f"/rest/v1/sanctions_names?list_code=eq.{list_code}"
           + f"&refreshed_at=lt.{quote(stamp, safe='')}")
    key = env["SUPABASE_SERVICE_KEY"]
    p = subprocess.run(
        ["curl", "-sS", "--max-time", "120", "-X", "DELETE", url,
         "-H", f"apikey: {key}", "-H", f"Authorization: Bearer {key}",
         "-H", "Prefer: return=representation,count=exact"],
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=180)
    if p.returncode != 0:
        print(f"  {list_code}: sweep FAILED (curl {p.returncode}) - stale rows remain")
        return 0
    body = (p.stdout or "").strip()
    if body.startswith("{"):
        print(f"  {list_code}: sweep REJECTED by the database: {body[:180]}")
        return 0
    try:
        n = len(json.loads(body)) if body.startswith("[") else 0
    except ValueError:
        n = 0
    if n:
        print(f"  {list_code}: swept {n} delisted name(s)")
    return n


def main(argv: list[str]) -> int:
    env = _env()
    if not env.get("SUPABASE_URL"):
        print("no SUPABASE_URL")
        return 2

    if "--check" in argv:
        for code in ("EU", "UK"):
            print(f"  {code}: {_count(env, code)} name(s) indexed")
        return 0

    from datetime import datetime, timezone
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    total = 0
    for code, url, parser in (("EU", _EU_CSV_URL, _eu_parse),
                              ("UK", _UK_CSV_URL, _uk_parse)):
        print(f"\n{code}: downloading")
        raw = _fetch(url)
        if not raw.strip():
            print(f"  {code}: EMPTY RESPONSE - skipping. The existing rows are "
                  f"left alone; a stale list beats an empty one.")
            continue
        records = parser(raw)
        rows = _rows_for(records, code, stamp)
        print(f"  parsed {len(records)} record(s) -> {len(rows)} unique key(s)")
        if not rows:
            print(f"  {code}: PARSED TO NOTHING - the feed format probably "
                  f"changed. Refusing to write, so the good rows survive.")
            continue
        n = _upsert(env, rows)
        print(f"  upserted {n}")
        if n == len(rows):
            _sweep_delisted(env, code, stamp, n)
        else:
            print(f"  {code}: upsert stopped at {n}/{len(rows)} - NOT sweeping, "
                  f"because 'not seen this run' is not trustworthy after a "
                  f"partial write.")
        total += n

    print("\nindexed now:")
    for code in ("EU", "UK"):
        print(f"  {code}: {_count(env, code)}")
    return 0 if total else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
