"""
An in-memory stand-in for `public.sanctions_names`, with the PostgREST filter
operators screen_sanctions actually uses.

WHY A FAKE AND NOT A MOCK. The Arabic/transliteration path asks the index for
rows by `eq`, by token containment (`cs.{a,b}`), and by an ILIKE pattern over the
sorted token string. A hand-written mock that returns canned rows would pass
whatever the query asked for, and the query is half of what that code does. This
fake EVALUATES the filter against the rows it holds, so a wrong pattern, a wrong
key or a wrong operator returns the wrong rows here exactly as it would from
Postgres.

It is the same seam test_country_never_removes_a_match.py uses (the two
storage-layer readers are replaced; `_screen_list_db`, `_list_refreshed_at` and
`handle_screen_sanctions` all run for real), made reusable. No test using it
touches the network or a database.

Operators supported: `eq` (also the bare value), `cs`, `ov`, `ilike`. Anything
else raises, so the fake can never silently agree with a query it does not
understand.
"""
from __future__ import annotations

import os
import re
import sys
from functools import lru_cache
from datetime import datetime, timezone
from typing import Any, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _parse_array(text: str) -> list[str]:
    inner = text[text.index("{") + 1: text.rindex("}")]
    return [t for t in inner.split(",") if t != ""]


@lru_cache(maxsize=256)
def _ilike_regex(pattern: str) -> "re.Pattern[str]":
    # PostgREST: `*` stands for `%`; `%` and `_` are the SQL wildcards.
    parts = re.split(r"[*%]", pattern)
    body = ".*".join(re.escape(p) for p in parts)
    return re.compile("^" + body + "$", re.IGNORECASE | re.DOTALL)


@lru_cache(maxsize=256)
def _posix_regex(pattern: str, ignore_case: bool) -> "re.Pattern[str]":
    """Postgres regex -> Python re, for the subset screen_sanctions emits: the
    start-of-word and end-of-word escapes become word boundaries; groups, classes,
    alternation, ? and + are common to both."""
    bs = chr(92)
    py = pattern.replace(bs + "m", bs + "b").replace(bs + "M", bs + "b")
    return re.compile(py, re.IGNORECASE if ignore_case else 0)


class FakeSanctionsTable:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.calls: list[dict] = []

    # ---- construction --------------------------------------------------------
    @classmethod
    def from_records(cls, records: dict[str, list[dict]], stamp: Optional[str] = None,
                     include_arabic: bool = True) -> "FakeSanctionsTable":
        """Build the table exactly as scripts/refresh_sanctions_lists.py does:
        records per list code ("EU", "UK") -> rows, through its own _rows_for."""
        scripts = os.path.join(ROOT, "scripts")
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        import refresh_sanctions_lists as rsl            # noqa: WPS433
        from core import arabic_names as _ar

        stamp = stamp or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        rows: list[dict] = []
        for code, recs in records.items():
            if not include_arabic:
                recs = [r for r in recs if not _ar.has_arabic_script(r["name"])]
            rows.extend(rsl._rows_for(recs, code, stamp))
        return cls(rows)

    # ---- the two storage readers ---------------------------------------------
    _COND = re.compile(r'(\w+)\.(imatch|match|ilike|eq)\.("(?:[^"\\]|\\.)*"|[^,)]+)')

    def _predicate(self, col: str, val: Any):
        """Compile ONE filter into a function of a row. Compiled once per call to
        select_rows_strict, not once per row: the real list is ~40,000 rows and
        a measurement run makes hundreds of calls."""
        sval = str(val)
        if col in ("and", "or"):
            conds = self._COND.findall(sval)
            assert conds, f"fake index cannot read logic tree {sval!r}"
            preds = []
            for c, op, v in conds:
                if v.startswith('"'):
                    v = v[1:-1]
                preds.append(self._predicate(c, f"{op}.{v}"))
            if col == "and":
                return lambda row: all(p(row) for p in preds)
            return lambda row: any(p(row) for p in preds)
        head = sval.split(".", 1)[0]
        if head == "cs":
            want = set(_parse_array(sval))
            return lambda row: want <= set(row.get(col) or [])
        if head == "ov":
            want = set(_parse_array(sval))
            return lambda row: bool(want & set(row.get(col) or []))
        if head == "ilike":
            rx = _ilike_regex(sval[len("ilike."):])
            return lambda row: bool(rx.match(str(row.get(col) or "")))
        if head in ("match", "imatch"):
            rx = _posix_regex(sval.split(".", 1)[1], head == "imatch")
            return lambda row: bool(rx.search(str(row.get(col) or "")))
        if head == "eq":
            want_s = sval[len("eq."):]
            return lambda row: str(row.get(col)) == want_s
        if head in ("neq", "gt", "gte", "lt", "lte", "like", "is", "in", "not",
                    "cd", "sl", "sr", "nxr", "nxl", "adj"):
            raise NotImplementedError(f"fake index does not implement {head!r}")
        return lambda row: str(row.get(col)) == sval

    def _match(self, row: dict, col: str, val: Any) -> bool:
        return self._predicate(col, val)(row)

    async def select_rows_strict(self, table: str, filters: Optional[dict] = None,
                                 limit: int = 1000, order: Optional[str] = None,
                                 gte: Optional[dict] = None, offset: int = 0
                                 ) -> list[dict]:
        assert table == "sanctions_names", table
        self.calls.append({"filters": dict(filters or {}), "limit": limit,
                           "order": order})
        rows = self.rows
        # Apply the cheap, selective equality filters first (list_code halves the
        # table), the regex ones last. The result is the same set either way.
        items = sorted((filters or {}).items(),
                       key=lambda kv: 0 if str(kv[1]).split(".", 1)[0] in ("eq",)
                       or "." not in str(kv[1]) else 1)
        for col, val in items:
            pred = self._predicate(col, val)
            rows = [r for r in rows if pred(r)]
        if order:
            col, _, direction = order.partition(".")
            rows = sorted(rows, key=lambda r: str(r.get(col, "")),
                          reverse=(direction == "desc"))
        return [dict(r) for r in rows[offset: offset + limit]]

    async def select_rows(self, table: str, **kw) -> list[dict]:
        return await self.select_rows_strict(table, **kw)

    # ---- wiring ----------------------------------------------------------------
    def install(self, monkeypatch, ss_module, sb_module) -> None:
        monkeypatch.setattr(sb_module, "select_rows_strict", self.select_rows_strict)
        monkeypatch.setattr(sb_module, "select_rows", self.select_rows)
        ss_module._age_cache.clear()

    def patterns_queried(self) -> list[str]:
        return [str(c["filters"].get("name_key")) for c in self.calls
                if str(c["filters"].get("name_key", "")).startswith("ilike.")]
