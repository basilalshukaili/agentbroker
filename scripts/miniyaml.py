#!/usr/bin/env python3
"""A deliberately small YAML reader for agentbroker/registry/servers.yaml.

WHY NOT PyYAML. This repository does not depend on it, and the file that reads
our public addresses is run by scheduled tasks on two machines. A missing
third-party import there is an outage nobody is watching for. The subset used by
servers.yaml is small enough to read correctly in 90 lines.

WHY IT IS ITS OWN FILE. The first attempt lived inside gen_manifests.py and was
wrong in a way that would not have raised: nested keys under `defaults:` landed
in the ROOT map, so `defaults['canonical_host']` would have been missing and the
generator would have crashed - or worse, silently taken a default from elsewhere.
Silently misreading the file that defines where strangers reach us is the exact
failure the generator exists to stop, so the reader gets its own module and its
own self-test.

    python scripts/miniyaml.py        # run the self-test

WHAT IT UNDERSTANDS: nested maps, lists of maps, inline `{a: b}` maps, empty
`[]`, quoted and bare scalars, ints, booleans, whole-line comments. It raises on
anything else rather than guessing. It also folds `>` and `|` block scalars. It
does NOT understand anchors, or a `#` comment on the end of a value line - a `#`
inside a URL is far more likely in this file than a trailing comment, and
guessing between them is how you lose a fragment off an address.
"""
from __future__ import annotations

import io
import re
import sys


def _scalar(v: str):
    v = v.strip()
    if not v:
        return None
    if len(v) >= 2 and v[0] in "\"'" and v[-1] == v[0]:
        return v[1:-1]
    low = v.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    if low in ("null", "~"):
        return None
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    return v


def _inline_map(v: str) -> dict:
    body = v.strip()
    if not (body.startswith("{") and body.endswith("}")):
        raise ValueError(f"not an inline map: {v!r}")
    body = body[1:-1].strip()
    out: dict = {}
    if not body:
        return out
    for part in body.split(","):
        if ":" not in part:
            raise ValueError(f"cannot read inline map entry {part!r}")
        k, val = part.split(":", 1)
        out[k.strip()] = _scalar(val)
    return out


def _prepare(text: str) -> list[tuple[int, str, bool]]:
    """(column, content, starts_a_list_item) per meaningful line.

    A `- ` prefix is rewritten away and its column shifted right by two, so the
    parser sees an ordinary map line that happens to be flagged as the first line
    of a new item. This is what makes both indented and flush list styles work.

    A `key: >` or `key: |` block scalar is folded here into a single quoted line,
    so the parser below never has to know about them. Descriptions long enough to
    need one are exactly the fields that must not be split across two files.
    """
    src = text.splitlines()
    out = []
    n = 0
    while n < len(src):
        raw = src[n]
        n += 1
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        stripped = raw.strip()
        if stripped.endswith(": >") or stripped.endswith(": |"):
            style = stripped[-1]
            col = len(raw) - len(raw.lstrip())
            key_part = stripped[:-2].rstrip()      # "key:" with the marker gone
            chunk: list[str] = []
            while n < len(src):
                nxt = src[n]
                if nxt.strip() and (len(nxt) - len(nxt.lstrip())) <= col:
                    break
                chunk.append(nxt.strip())
                n += 1
            while chunk and not chunk[-1]:
                chunk.pop()
            joined = ("\n" if style == "|" else " ").join(chunk)
            if not joined:
                raise ValueError(f"empty block scalar for {key_part!r}")
            if '"' in joined:
                raise ValueError(f"block scalar for {key_part!r} contains a quote; "
                                 f"this reader cannot escape it")
            new_item = key_part.startswith("- ")
            if new_item:
                key_part = key_part[2:].strip()
                col += 2
            out.append((col, f'{key_part} "{joined}"', new_item))
            continue
        if "\t" in raw[: len(raw) - len(raw.lstrip())]:
            raise ValueError(f"line {n}: tab indentation is not valid YAML")
        col = len(raw) - len(raw.lstrip())
        body = raw.strip()
        new_item = False
        if body == "-":
            raise ValueError(f"line {n}: a bare '-' is not supported")
        if body.startswith("- "):
            new_item = True
            body = body[2:].strip()
            col += 2
        out.append((col, body, new_item))
    return out


def _parse_block(rows, pos: int, col: int):
    """Parse everything at column `col`. Returns (value, next_pos)."""
    if rows[pos][2]:
        items = []
        while pos < len(rows) and rows[pos][0] == col and rows[pos][2]:
            value, pos = _parse_item(rows, pos, col)
            items.append(value)
        return items, pos
    out: dict = {}
    while pos < len(rows) and rows[pos][0] == col and not rows[pos][2]:
        pos = _parse_key(out, rows, pos, col)
    return out, pos


def _parse_item(rows, pos: int, col: int):
    _, body, _ = rows[pos]
    if body.startswith("{"):
        return _inline_map(body), pos + 1
    if ":" not in body:
        return _scalar(body), pos + 1
    entry: dict = {}
    pos = _parse_key(entry, rows, pos, col)          # the dash line itself
    while pos < len(rows) and rows[pos][0] == col and not rows[pos][2]:
        pos = _parse_key(entry, rows, pos, col)      # the item's remaining keys
    return entry, pos


def _parse_key(target: dict, rows, pos: int, col: int) -> int:
    _, body, _ = rows[pos]
    key, sep, value = body.partition(":")
    if not sep:
        raise ValueError(f"cannot read line: {body!r}")
    key, value = key.strip(), value.strip()
    if key in target:
        raise ValueError(f"duplicate key {key!r}")
    pos += 1
    if value == "":
        if pos < len(rows) and rows[pos][0] > col:
            target[key], pos = _parse_block(rows, pos, rows[pos][0])
        else:
            target[key] = None
    elif value.startswith("{"):
        target[key] = _inline_map(value)
    elif value == "[]":
        target[key] = []
    else:
        target[key] = _scalar(value)
    return pos


def loads(text: str):
    rows = _prepare(text)
    if not rows:
        return {}
    value, pos = _parse_block(rows, 0, rows[0][0])
    if pos != len(rows):
        raise ValueError(f"stopped at line {rows[pos][1]!r}; indentation is inconsistent")
    return value


def load(path: str):
    return loads(io.open(path, encoding="utf-8").read())


# --------------------------------------------------------------------------
# Self-test. A reader that quietly returns the wrong shape is worse than one
# that crashes, so this asserts on the exact structures servers.yaml uses,
# INCLUDING the nested-map case the first implementation got wrong.
# --------------------------------------------------------------------------
SAMPLE = """\
# a comment
version: 1

defaults:
  namespace: dev.hatchloop
  # nested keys must land under defaults, not at the root
  canonical_host: https://hatchloop.dev
  count: 7
  enabled: true

servers:
  - slug: alpha
    title: "Alpha: with a colon"
    live: false
    publish: {registry: true, smithery: false}
    tags: []
  - slug: beta
    nested:
      deep: yes
    blurb: >
      folded onto
      one line
    tags:
      - one
      - two
"""


def _selftest() -> int:
    d = loads(SAMPLE)
    checks = [
        ("root keys", sorted(d) == ["defaults", "servers", "version"]),
        ("scalar int at root", d["version"] == 1),
        ("nested map is nested", d["defaults"]["namespace"] == "dev.hatchloop"),
        ("nested key not leaked to root", "namespace" not in d),
        ("url survives whole", d["defaults"]["canonical_host"] == "https://hatchloop.dev"),
        ("nested int", d["defaults"]["count"] == 7),
        ("nested bool", d["defaults"]["enabled"] is True),
        ("list length", len(d["servers"]) == 2),
        ("list item key", d["servers"][0]["slug"] == "alpha"),
        ("quoted value keeps its colon", d["servers"][0]["title"] == "Alpha: with a colon"),
        ("false is false not string", d["servers"][0]["live"] is False),
        ("inline map", d["servers"][0]["publish"] == {"registry": True, "smithery": False}),
        ("empty list", d["servers"][0]["tags"] == []),
        ("second item", d["servers"][1]["slug"] == "beta"),
        ("map inside a list item", d["servers"][1]["nested"]["deep"] is True),
        ("folded block scalar", d["servers"][1]["blurb"] == "folded onto one line"),
        ("list of scalars", d["servers"][1]["tags"] == ["one", "two"]),
    ]
    bad = [name for name, ok in checks if not ok]

    # It must also REFUSE what it cannot read, or it is a gate that inspects
    # nothing. Each of these has to raise.
    must_raise = [
        "a: b\n\tc: d\n",          # tab indent
        "no colon here\n",         # not a key
        "a: {b}\n",                # malformed inline map
        "a: 1\na: 2\n",            # duplicate key
        "a: >\n",                  # a block scalar with no body
        'a: >\n  has a " quote\n',  # cannot be re-quoted safely
    ]
    for src in must_raise:
        try:
            loads(src)
        except ValueError:
            continue
        bad.append(f"accepted invalid input {src!r}")

    if bad:
        print(f"miniyaml SELF-TEST FAILED ({len(bad)}):")
        for b in bad:
            print(f"  {b}")
        return 1
    print(f"miniyaml self-test ok: {len(checks)} structure checks, "
          f"{len(must_raise)} refusals")
    return 0


if __name__ == "__main__":
    sys.exit(_selftest())
