#!/usr/bin/env python3
"""Runs ON the box. Finds - and with --apply, blanks - key-header values already written to Caddy logs.

    python3 remote_scrub_logs.py                # DRY RUN: counts only
    python3 remote_scrub_logs.py --apply        # blank them, then re-scan and report what is left

WHY: until the log filter in deploy/caddy/mcp_direct.py is live, Caddy writes the `X-Agent-Identity`
request header (the one every key we issue tells people to send) to /var/log/caddy/hatchloop.dev*.log in
clear text. The retained logs held only a placeholder when audited (2026-09-30); this removes even that,
and would remove a real key if one had been presented since.

NEVER PRINTS A HEADER VALUE. Output is counts per file and per class (key-shaped / other).

HOW IT BLANKS, because Caddy holds the active log open for append:
  * uncompressed files (the active log and any plain rolled one) are edited IN PLACE with a
    SAME-LENGTH replacement ('*' x len), so no byte moves and nothing Caddy appends after our read is
    disturbed - no truncate, no rename, no lost lines, still valid JSON;
  * rolled .gz files are not open by anyone: rewritten to a temp file in the same directory and
    atomically replaced, keeping owner and mode.
"""
from __future__ import annotations

import glob
import gzip
import os
import re
import sys

LOG_DIR = "/var/log/caddy"
PATTERNS = ["hatchloop.dev*.log*", "api.hatchloop.dev*.log*"]

# "X-Agent-Identity":["v1","v2"]   (Caddy writes header values as arrays, compact JSON)
HEADER_ARRAY = re.compile(
    rb'("(?i:x-agent-identity|x-api-key)"\s*:\s*\[)((?:\s*"(?:[^"\\]|\\.)*"\s*,?)*)(\s*\])')
STRING = re.compile(rb'"((?:[^"\\]|\\.)*)"')
KEY_SHAPED = re.compile(rb"^[A-Za-z0-9_\-]{8,}\.[0-9a-f]{64}$")
STARS = ord("*")


def _blank_strings(array_body: bytes, tally: dict | None) -> bytes:
    def one(m: "re.Match[bytes]") -> bytes:
        val = m.group(1)
        if val == b"REDACTED" or set(val) <= {STARS}:
            return m.group(0)                                    # already clean
        if tally is not None:
            tally["key_shaped" if KEY_SHAPED.match(val) else "other"] += 1
        return b'"' + b"*" * len(val) + b'"'
    return STRING.sub(one, array_body)


def blank_bytes(data: bytes, tally: dict | None = None) -> bytes:
    """`data` with every key-header value replaced by same-length stars. Pure; same length always."""
    def one(m: "re.Match[bytes]") -> bytes:
        return m.group(1) + _blank_strings(m.group(2), tally) + m.group(3)
    out = HEADER_ARRAY.sub(one, data)
    assert len(out) == len(data), "same-length invariant broken"
    return out


def _new_tally() -> dict:
    return {"key_shaped": 0, "other": 0}


def scan_bytes(data: bytes) -> dict:
    t = _new_tally()
    blank_bytes(data, t)
    return t


def _files() -> list:
    seen, out = set(), []
    for pat in PATTERNS:
        for p in sorted(glob.glob(os.path.join(LOG_DIR, pat))):
            if p not in seen and os.path.isfile(p):
                seen.add(p)
                out.append(p)
    return out


def _read(path: str) -> bytes:
    if path.endswith(".gz"):
        with gzip.open(path, "rb") as fh:
            return fh.read()
    with open(path, "rb") as fh:
        return fh.read()


def _apply_plain(path: str, original: bytes) -> int:
    """In-place, same length. Writes only the byte ranges that change."""
    n = 0
    with open(path, "r+b") as fh:
        for m in HEADER_ARRAY.finditer(original):
            new = m.group(1) + _blank_strings(m.group(2), None) + m.group(3)
            if new != m.group(0):
                fh.seek(m.start())
                fh.write(new)
                n += 1
        fh.flush()
        os.fsync(fh.fileno())
    return n


def _apply_gz(path: str, original: bytes) -> int:
    blanked = blank_bytes(original)
    if blanked == original:
        return 0
    st = os.stat(path)
    tmp = path + ".scrub.tmp"
    with gzip.open(tmp, "wb", compresslevel=6) as fh:
        fh.write(blanked)
    os.chown(tmp, st.st_uid, st.st_gid)
    os.chmod(tmp, st.st_mode & 0o7777)
    os.replace(tmp, path)
    return 1


def main(argv: list) -> int:
    apply = "--apply" in argv
    files = _files()
    if not files:
        print(f"no log files matched under {LOG_DIR}")
        return 0
    total = _new_tally()
    changed_files = 0
    for path in files:
        try:
            data = _read(path)
        except Exception as exc:  # noqa: BLE001 - one unreadable file must not hide the rest
            print(f"{os.path.basename(path)}: UNREADABLE ({type(exc).__name__})")
            continue
        t = scan_bytes(data)
        hits = t["key_shaped"] + t["other"]
        total["key_shaped"] += t["key_shaped"]
        total["other"] += t["other"]
        line = f"{os.path.basename(path)}: unredacted key-header values: key_shaped={t['key_shaped']} other={t['other']}"
        if apply and hits:
            if path.endswith(".gz"):
                _apply_gz(path, data)
            else:
                _apply_plain(path, data)
            after = scan_bytes(_read(path))
            left = after["key_shaped"] + after["other"]
            line += f"  -> blanked; remaining={left}"
            changed_files += 1
            if left:
                print(line)
                print("FAILED: values remain after blanking; stopping")
                return 1
        print(line)
    print(f"TOTAL unredacted: key_shaped={total['key_shaped']} other={total['other']} "
          f"in {len(files)} file(s); {'blanked in ' + str(changed_files) + ' file(s)' if apply else 'DRY RUN, nothing changed'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
