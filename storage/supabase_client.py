"""
Thin Supabase REST client using httpx — no supabase-py SDK dependency.

Uses the Supabase PostgREST REST API directly so it works in any environment
where httpx is available (which it is, since it's in requirements.txt).

Key design decisions:
- Async-first (AsyncClient); sync wrapper for startup / import-time calls.
- Never raises on network errors — returns None/False + logs. Callers treat
  Supabase as an optional durable layer; in-memory state always works.
- Service-key auth (full access, server-side only). Never exposed to clients.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger("smb_broker.supabase_client")

_SUPABASE_URL: str = ""
_SUPABASE_KEY: str = ""


def _get_config() -> tuple[str, str]:
    """Lazy-load config from env so imports don't fail if vars aren't set yet."""
    url = os.getenv("SUPABASE_URL", "").rstrip("/")
    key = os.getenv("SUPABASE_SERVICE_KEY", "") or os.getenv("SUPABASE_ANON_KEY", "")
    return url, key


def _headers(key: str) -> dict[str, str]:
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


async def rpc(fn: str, payload: dict[str, Any]) -> Any:
    """
    Call a Supabase Postgres function via PostgREST: POST /rest/v1/rpc/{fn}.

    RAISES on any failure -- callers on the spend path must fail closed.
    Unlike insert_row's fail-open design (billing is optional), RPC calls
    gate real money: if we cannot reach Supabase, we MUST NOT do paid work.

    Returns the parsed JSON response body (typically a dict from JSONB functions).
    """
    url, key = _get_config()
    if not url or not key:
        raise RuntimeError(
            f"rpc({fn!r}) aborted: SUPABASE_URL or service key not configured. "
            "Cannot proceed on spend path without Supabase."
        )
    import httpx
    endpoint = f"{url}/rest/v1/rpc/{fn}"
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(endpoint, headers=headers, json=payload)
    except Exception as exc:
        raise RuntimeError(
            f"rpc({fn!r}) transport error: {exc}"
        ) from exc

    if resp.status_code not in (200, 201):
        raise RuntimeError(
            f"rpc({fn!r}) failed: HTTP {resp.status_code} body={resp.text[:400]}"
        )
    try:
        return resp.json()
    except Exception as exc:
        raise RuntimeError(
            f"rpc({fn!r}) JSON decode error: {exc} body={resp.text[:200]}"
        ) from exc


async def insert_row(table: str, row: dict[str, Any]) -> Optional[dict]:
    """
    Insert a single row into `table`. Returns the inserted row on success,
    None on any failure (missing config, network error, DB error).
    Never raises.
    """
    url, key = _get_config()
    if not url or not key:
        logger.debug("supabase_insert_skipped table=%s reason=missing_config", table)
        return None
    try:
        import httpx
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(
                f"{url}/rest/v1/{table}",
                headers=_headers(key),
                json=row,
            )
        if resp.status_code in (200, 201):
            data = resp.json()
            result = data[0] if isinstance(data, list) and data else data
            logger.debug("supabase_insert_ok table=%s id=%s", table, result.get("id"))
            return result
        logger.warning(
            "supabase_insert_failed table=%s status=%s body=%s",
            table, resp.status_code, resp.text[:200],
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("supabase_insert_exception table=%s err=%s", table, exc)
    return None


async def upsert_row(table: str, row: dict[str, Any], on_conflict: str = "id") -> Optional[dict]:
    """
    Upsert a single row. `on_conflict` is the column name for conflict resolution.
    Returns the upserted row on success, None on failure. Never raises.
    """
    url, key = _get_config()
    if not url or not key:
        logger.debug("supabase_upsert_skipped table=%s reason=missing_config", table)
        return None
    try:
        import httpx
        headers = _headers(key)
        headers["Prefer"] = f"resolution=merge-duplicates,return=representation"
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(
                f"{url}/rest/v1/{table}",
                headers=headers,
                json=row,
            )
        if resp.status_code in (200, 201):
            data = resp.json()
            result = data[0] if isinstance(data, list) and data else data
            logger.debug("supabase_upsert_ok table=%s", table)
            return result
        logger.warning(
            "supabase_upsert_failed table=%s status=%s body=%s",
            table, resp.status_code, resp.text[:200],
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("supabase_upsert_exception table=%s err=%s", table, exc)
    return None


# PostgREST operators a caller may supply inline. Deliberately a closed list:
# see the note in select_rows about values that merely contain a dot.
_PASSTHROUGH_OPS = frozenset({
    "eq", "neq", "gt", "gte", "lt", "lte", "like", "ilike",
    "is", "in", "cs", "cd", "ov", "sl", "sr", "nxr", "nxl", "adj", "not",
})


class SupabaseUnavailable(RuntimeError):
    """The query did not run. NOT the same as "the query returned nothing"."""


# WHY THE STRICT VARIANTS EXIST.
#
# select_rows() returns [] on every failure - no config, network error, non-200
# - and is contractually incapable of raising. That is the right default for a
# display path: a dashboard should degrade, not crash.
#
# It is the WRONG default for a safety path, and worse, it makes the correct
# defence look like it is present. Callers across this codebase wrote
#
#     try:
#         rows = await select_rows(...)
#     except Exception:
#         rows = None            # distinguish failure from empty
#
# and every one of those handlers is DEAD CODE. I wrote one of them myself,
# this morning, in the sanctions screen - as the fix for a bug whose whole
# lesson was that an empty result and an unavailable source must never look
# alike. The guard could not fire.
#
# Found by an adversarial review of the same day's work. The instances that
# matter: a refunded customer keeps paid access because the revocation list
# read as empty; opt-outs un-suppress after a redeploy because hydration read
# as empty; a sanctions list reports as SCREENED because the index read as
# empty.
#
# So: strict variants that RAISE, for the paths where "I could not check" must
# never be mistaken for "I checked and there was nothing".


async def select_rows_strict(table: str, **kw) -> list[dict]:
    """select_rows, but a failed query RAISES instead of returning []."""
    url, key = _get_config()
    if not url or not key:
        raise SupabaseUnavailable(f"no Supabase config; {table} was not queried")
    try:
        import httpx
        params = _select_params(**kw)
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(
                f"{url}/rest/v1/{table}",
                headers={"apikey": key, "Authorization": f"Bearer {key}"},
                params=params,
            )
    except Exception as exc:                    # noqa: BLE001
        raise SupabaseUnavailable(f"{table} unreachable: {exc}") from exc
    if resp.status_code != 200:
        raise SupabaseUnavailable(
            f"{table} returned HTTP {resp.status_code}, so it was not queried")
    return resp.json() or []


def select_rows_sync_strict(table: str, **kw) -> list[dict]:
    """Synchronous select_rows_strict, for import-time and non-async callers."""
    url, key = _get_config()
    if not url or not key:
        raise SupabaseUnavailable(f"no Supabase config; {table} was not queried")
    try:
        import httpx
        with httpx.Client(timeout=8.0) as client:
            resp = client.get(
                f"{url}/rest/v1/{table}",
                headers={"apikey": key, "Authorization": f"Bearer {key}"},
                params=_select_params(**kw),
            )
    except Exception as exc:                    # noqa: BLE001
        raise SupabaseUnavailable(f"{table} unreachable: {exc}") from exc
    if resp.status_code != 200:
        raise SupabaseUnavailable(
            f"{table} returned HTTP {resp.status_code}, so it was not queried")
    return resp.json() or []


def _select_params(filters=None, limit: int = 1000, order=None, gte=None) -> dict:
    """Query-string builder shared by the lenient and strict readers, so the
    two can never encode a filter differently."""
    params: dict[str, Any] = {"limit": limit}
    if order:
        params["order"] = order
    if gte:
        for col, val in gte.items():
            params[col] = f"gte.{val}"
    if filters:
        for col, val in filters.items():
            sval = str(val)
            params[col] = sval if sval.split(".", 1)[0] in _PASSTHROUGH_OPS                 else f"eq.{val}"
    return params


async def select_rows(
    table: str,
    filters: Optional[dict[str, Any]] = None,
    limit: int = 1000,
    order: Optional[str] = None,
    gte: Optional[dict[str, Any]] = None,
) -> list[dict]:
    """
    Select rows from `table` with optional equality filters.

    `order` (e.g. "created_at.desc") and `gte` (e.g. {"created_at": iso}) exist
    because a bare LIMIT without ORDER BY returns an ARBITRARY slice in
    PostgREST - which silently broke rate-limit counting and thread-uniqueness
    checks over large tables (adversarial review 2026-08-26). Prefer bounding the
    query rather than filtering a truncated sample client-side.

    Returns list of rows (may be empty). Never raises.
    """
    url, key = _get_config()
    if not url or not key:
        logger.debug("supabase_select_skipped table=%s reason=missing_config", table)
        return []
    try:
        import httpx
        params: dict[str, Any] = {"limit": limit}
        if order:
            params["order"] = order
        if gte:
            for col, val in gte.items():
                params[col] = f"gte.{val}"
        if filters:
            for col, val in filters.items():
                # A filter value may carry its OWN PostgREST operator. Equality
                # covers almost every caller, so it stays the default - but the
                # sanctions index needs array containment (`cs.{a,b}`) to find
                # entries whose tokens include every token of the query, and
                # forcing `eq.` onto that produced `eq.cs.{...}`, which matches
                # nothing and fails silently.
                #
                # Pass through only RECOGNISED operators. An unknown prefix is
                # far more likely to be a value that happens to contain a dot -
                # a hostname, a version, a filename - than an operator someone
                # meant, and treating those as operators would quietly change
                # what existing queries return.
                sval = str(val)
                if sval.split(".", 1)[0] in _PASSTHROUGH_OPS:
                    params[col] = sval
                else:
                    params[col] = f"eq.{val}"
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(
                f"{url}/rest/v1/{table}",
                headers={
                    "apikey": key,
                    "Authorization": f"Bearer {key}",
                },
                params=params,
            )
        if resp.status_code == 200:
            return resp.json() or []
        logger.warning(
            "supabase_select_failed table=%s status=%s", table, resp.status_code
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("supabase_select_exception table=%s err=%s", table, exc)
    return []


def select_rows_sync(
    table: str,
    filters: Optional[dict[str, Any]] = None,
    limit: int = 1000,
) -> list[dict]:
    """
    Synchronous version of select_rows for use at module import time.
    Uses httpx.Client (blocking). Never raises.
    """
    url, key = _get_config()
    if not url or not key:
        logger.debug("supabase_select_sync_skipped table=%s reason=missing_config", table)
        return []
    try:
        import httpx
        params: dict[str, Any] = {"limit": limit}
        if filters:
            for col, val in filters.items():
                sval = str(val)
                if sval.split(".", 1)[0] in _PASSTHROUGH_OPS:
                    params[col] = sval
                else:
                    params[col] = f"eq.{val}"
        with httpx.Client(timeout=8.0) as client:
            resp = client.get(
                f"{url}/rest/v1/{table}",
                headers={
                    "apikey": key,
                    "Authorization": f"Bearer {key}",
                },
                params=params,
            )
        if resp.status_code == 200:
            return resp.json() or []
        logger.warning(
            "supabase_select_sync_failed table=%s status=%s", table, resp.status_code
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("supabase_select_sync_exception table=%s err=%s", table, exc)
    return []
