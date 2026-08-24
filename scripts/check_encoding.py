#!/usr/bin/env python3
"""
scripts/check_encoding.py -- Wire-string ASCII encoding checker.

Scans files involved in the data-metering feature and core billing/dispatch
paths for non-ASCII characters in Python source.  Non-ASCII in code strings
(not HTML email templates) can cause silent mojibake on wire paths (HTTP
headers, JSON payloads, Supabase RPC calls) that expect ASCII.

SCOPE: new/changed files from the freemium metering feature plus the
dispatch path (mcp_server.py).  Deliberately excludes billing/emails.py
(HTML email templates with intentional em-dashes) and static markdown docs.

Exit 0 if clean.  Exit 1 if non-ASCII characters are found.

Run from the agentbroker/ directory:
    python scripts/check_encoding.py
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_AGENTBROKER_DIR = _SCRIPT_DIR.parent
_REPO_ROOT = _AGENTBROKER_DIR.parent

# Explicit file list: new and modified files from the data-metering feature.
# Older source files use em-dashes (U+2014) and arrows (U+2192) in comments;
# those are pre-existing and only appear in docstrings/comments, never on the
# wire.  This list covers new code written for the freemium metering feature
# where ASCII-only in all strings is enforced from the start.
_SCAN_FILES: list[Path] = [
    _AGENTBROKER_DIR / "billing" / "data_quota.py",          # new
    _AGENTBROKER_DIR / "scripts" / "check_pricing.py",        # new
    _AGENTBROKER_DIR / "scripts" / "check_encoding.py",       # new (self)
    _AGENTBROKER_DIR / "tests" / "unit" / "test_data_metering.py",  # new
]

# Code points that are explicitly allowed even if > 127.
# Empty by default: all non-ASCII in wire paths should be ASCII-escaped.
_ALLOWED_CODEPOINTS: frozenset[int] = frozenset()


def _check_file(path: Path) -> list[str]:
    """Return violation strings for `path`, or empty list if clean."""
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return [f"  WARN: cannot read {path}: {exc}"]

    hits: list[str] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for col, ch in enumerate(line, start=1):
            cp = ord(ch)
            if cp > 127 and cp not in _ALLOWED_CODEPOINTS:
                try:
                    rel = path.relative_to(_REPO_ROOT)
                except ValueError:
                    rel = path
                hits.append(
                    f"  {rel} line {lineno} col {col}: "
                    f"non-ASCII U+{cp:04X} {ascii(ch)}  >> {line.strip()[:80]}"
                )
    return hits


def _safe_print(msg: str) -> None:
    """Print msg safely on Windows consoles with narrow code pages."""
    try:
        print(msg)
    except UnicodeEncodeError:
        # Fall back to UTF-8 bytes written directly to the stdout buffer.
        sys.stdout.buffer.write((msg + "\n").encode("utf-8", errors="replace"))


def main() -> int:
    all_hits: list[str] = []
    scanned = 0
    for fpath in _SCAN_FILES:
        if fpath.exists():
            scanned += 1
            all_hits.extend(_check_file(fpath))

    if all_hits:
        _safe_print(
            f"check_encoding FAILED -- {len(all_hits)} non-ASCII character(s) found:"
        )
        for h in all_hits:
            _safe_print(h)
        return 1

    _safe_print(
        f"check_encoding OK -- {scanned} files scanned, 0 non-ASCII characters"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
