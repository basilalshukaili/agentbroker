#!/usr/bin/env python3
r"""Detect / repair UTF-8-as-cp1252 mojibake in agentbroker text files.

A tool once wrote a file by encoding UTF-8 bytes through cp1252, so smart
punctuation like the em-dash (U+2014) turned into a multi-char mojibake run
(e.g. U+00E2 U+20AC U+201D). The health monitor's check_encoding flags these
(reported 106 in manifest.json). We repair by the exact inverse of the
corruption, per known punctuation character - and this source stays pure ASCII
(the mojibake sequences are GENERATED from clean code points, never typed as
literals) so the fixer never trips the very check it serves, nor corrupts
itself on a re-run.

    python scripts/fix_mojibake.py            # report only
    python scripts/fix_mojibake.py --write    # repair in place
"""
from __future__ import annotations
import argparse
import os

# Clean "smart" characters that get mojibake'd on a bad UTF-8->cp1252 write.
_GOOD = [
    "—", "–", "‒",           # em / en / figure dash
    "‘", "’",                      # single quotes
    "“", "”",                      # double quotes
    "…",                                # ellipsis
    "•",                                # bullet
    " ",                                # nbsp
    "é", "è", "ü", "ö",  # accented latin sometimes seen
    "→",                                # arrow
]

# Build mojibake -> clean map by REPRODUCING the corruption: the original
# character's UTF-8 bytes, mis-decoded as cp1252, is exactly what ended up in
# the file. Only characters whose bytes are all valid cp1252 can round-trip.
_TABLE: dict[str, str] = {}
for _g in _GOOD:
    try:
        _bad = _g.encode("utf-8").decode("cp1252")
    except UnicodeDecodeError:
        continue
    if _bad != _g and _bad.isascii() is False:
        _TABLE[_bad] = _g
# longest first, so a 3-char run is fixed before any 2-char subrun
_ORDER = sorted(_TABLE, key=len, reverse=True)
# The common 2-char head of every run ("Ã¢" + Euro), derived (never typed) so
# this source stays pure ASCII: it is the first two chars of the em-dash run.
_MARKER = ("—".encode("utf-8").decode("cp1252"))[:2]

EXTS = (".json", ".md", ".py", ".txt", ".ts", ".js", ".html")
SKIP_DIRS = {"node_modules", ".git", ".next", "dist", "build", "__pycache__"}
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SELF = os.path.abspath(__file__)


def repair(text: str) -> tuple[str, int]:
    n = 0
    for bad in _ORDER:
        if bad in text:
            n += text.count(bad)
            text = text.replace(bad, _TABLE[bad])
    return text, n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()
    files = runs = 0
    for base, dirs, names in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for fn in names:
            if not fn.endswith(EXTS):
                continue
            p = os.path.join(base, fn)
            if os.path.abspath(p) == SELF:
                continue
            try:
                text = open(p, encoding="utf-8").read()
            except (OSError, UnicodeDecodeError):
                continue
            if _MARKER not in text and not any(b in text for b in _ORDER):
                continue
            fixed, n = repair(text)
            if n == 0:
                continue
            left = _MARKER in fixed
            files += 1
            runs += n
            rel = os.path.relpath(p, ROOT)
            # A residual marker means the file legitimately contains a mojibake
            # literal (a detection table, like this script or the snapshot
            # refresher) - never auto-rewrite those; report for a human.
            note = "   (residual marker - SKIPPED, likely a detection table)" if left else ""
            print(f"{n:4d}  {rel}{note}")
            if args.write and fixed != text and not left:
                open(p, "w", encoding="utf-8", newline="\n").write(fixed)
    print(f"\n{'REPAIRED' if args.write else 'FOUND'} {runs} mojibake run(s) in {files} file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
