"""
An in-memory model of the two Postgres functions behind the find_business free
trial: sql/agentbroker/008_anon_trial_reserve_release_rpc.sql.

WHY A MODEL AND NOT A MOCK. The counter's whole value is in its semantics -
lifetime per-caller rows, a per-UTC-day global row, both checked and bumped in
one step, a refused request consuming nothing, a release that cannot go below
zero. A `MagicMock` that returns `{"allowed": True}` proves the caller handles a
reply, not that the reply is right. This class re-states the SQL function
line for line, so the tests that run against it are testing the contract the
SQL has to keep, and the SQL file's structure is checked separately in
tests/unit/test_find_business_free_trial.py.

WHAT IT CANNOT PROVE: that the SQL parses and behaves the same in Postgres. No
Postgres is available where these tests run. The migration has to be applied,
and tests/integration/test_anon_trial_rpc_live.py run against the real
database, before a build that depends on it goes live.

It is the ENTIRE durable state of the trial: a test that wants to simulate a
restart drops every in-process object and keeps this one, exactly as a
container restart keeps Supabase.
"""
from __future__ import annotations

import re


class FakeTrialStore:
    """Callable with the same (fn, payload) shape as storage.supabase_client.rpc."""

    def __init__(self) -> None:
        # bucket_key -> {"count": int, "quota_date": str}. This is the
        # `anon_data_quota` table, restricted to the two columns the
        # functions touch.
        self.rows: dict[str, dict] = {}
        self.calls: list[tuple[str, dict]] = []
        # Set to an exception instance to make every call raise it.
        self.down: Exception | None = None
        # Set to a callable(fn, payload) -> reply to override a reply entirely.
        self.override = None

    # -- the rpc() seam ------------------------------------------------------
    async def __call__(self, fn: str, payload: dict):
        self.calls.append((fn, dict(payload)))
        if self.down is not None:
            raise self.down
        if self.override is not None:
            return self.override(fn, payload)
        if fn == "anon_trial_reserve":
            return self._reserve(**payload)
        if fn == "anon_trial_release":
            return self._release(**payload)
        # What PostgREST says for a function that does not exist.
        raise RuntimeError(f"rpc({fn!r}) failed: HTTP 404 body={{\"code\":\"PGRST202\"}}")

    # -- helpers -------------------------------------------------------------
    @staticmethod
    def _validate(p_tool, p_caller_key, p_day):
        if not (isinstance(p_tool, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", p_tool)):
            raise RuntimeError("rpc failed: HTTP 400 invalid tool")
        if not (isinstance(p_caller_key, str) and re.fullmatch(r"[0-9a-f]{64}", p_caller_key)):
            raise RuntimeError("rpc failed: HTTP 400 invalid caller key")
        if not (isinstance(p_day, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", p_day)):
            raise RuntimeError("rpc failed: HTTP 400 invalid day")

    def _row(self, key: str, date: str) -> dict:
        return self.rows.setdefault(key, {"count": 0, "quota_date": date})

    def count_for_caller(self, tool: str, caller_key: str) -> int:
        return self.rows.get(f"trial:c:{tool}:{caller_key}", {}).get("count", 0)

    def global_count(self, tool: str) -> int:
        return self.rows.get(f"trial:g:{tool}", {}).get("count", 0)

    # -- anon_trial_reserve --------------------------------------------------
    def _reserve(self, p_tool, p_caller_key, p_day, p_caller_limit, p_global_limit):
        self._validate(p_tool, p_caller_key, p_day)
        if (not isinstance(p_caller_limit, int) or p_caller_limit < 0
                or not isinstance(p_global_limit, int) or p_global_limit < 0):
            raise RuntimeError("rpc failed: HTTP 400 invalid limit")
        g = self._row(f"trial:g:{p_tool}", p_day)
        c = self._row(f"trial:c:{p_tool}:{p_caller_key}", "lifetime")

        g_count, g_date = g["count"], g["quota_date"]
        rolled = False
        # A NEW day rolls the global counter over. An OLDER day never rewinds it
        # (a stale clock must not be a way to reset the ceiling).
        if g_date is None or p_day > g_date:
            g_count, g_date, rolled = 0, p_day, True
        if rolled:
            g["count"], g["quota_date"] = g_count, g_date

        if c["count"] >= p_caller_limit:
            return {"allowed": False, "reason": "caller_limit",
                    "caller_count": c["count"], "global_count": g_count}
        if g_count >= p_global_limit:
            return {"allowed": False, "reason": "global_limit",
                    "caller_count": c["count"], "global_count": g_count}
        c["count"] += 1
        g["count"] = g_count + 1
        return {"allowed": True, "reason": "ok",
                "caller_count": c["count"], "global_count": g["count"]}

    # -- anon_trial_release --------------------------------------------------
    def _release(self, p_tool, p_caller_key, p_day):
        self._validate(p_tool, p_caller_key, p_day)
        g = self.rows.get(f"trial:g:{p_tool}")
        c = self.rows.get(f"trial:c:{p_tool}:{p_caller_key}")
        if c is None:
            return {"released": False}
        released = False
        if c["count"] > 0:
            c["count"] -= 1
            released = True
            # Only today's global slot can be given back; yesterday's reset
            # already forgot it.
            if g is not None and g["quota_date"] == p_day and g["count"] > 0:
                g["count"] -= 1
        return {"released": released}
