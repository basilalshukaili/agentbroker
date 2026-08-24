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


async def select_rows(
    table: str,
    filters: Optional[dict[str, Any]] = None,
    limit: int = 1000,
) -> list[dict]:
    """
    Select rows from `table` with optional equality filters.
    Returns list of rows (may be empty). Never raises.
    """
    url, key = _get_config()
    if not url or not key:
        logger.debug("supabase_select_skipped table=%s reason=missing_config", table)
        return []
    try:
        import httpx
        params: dict[str, Any] = {"limit": limit}
        if filters:
            for col, val in filters.items():
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
