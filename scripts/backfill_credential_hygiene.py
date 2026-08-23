"""
One-time backfill: replace raw-token prefixes (eyJ...) stored in
usage_events.key_id and operations.agent_id with the parsed agent_id.

Run once after deploying Fix 1 (credential hygiene).

How it works:
  1. Query both tables for rows where the value starts with 'eyJ' (base64url
     prefix of every token this system issues — sorted-key JSON, so agent_id
     always appears in the first ~40 chars of the payload, well within the
     64-char prefix that was being stored).
  2. For each distinct raw-prefix, decode the base64url payload fragment and
     extract the "agent_id" field via regex.
  3. UPDATE all matching rows with the parsed value.  Rows whose prefix cannot
     be decoded are set to 'legacy_redacted'.

Verification: after the run, assert zero rows remain with eyJ-prefixed values.
"""
from __future__ import annotations

import asyncio
import base64
import os
import re
import sys
import logging

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("backfill_cred")

# Load env from .env file if running locally
_env_path = os.path.join(os.path.dirname(__file__), "..", "..", ".env")
if os.path.exists(_env_path):
    with open(_env_path, encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())


def _decode_agent_id(raw_prefix: str) -> str:
    """
    Given the first N chars of a base64url-encoded JWT payload, decode
    enough to extract the "agent_id" field.  Returns 'legacy_redacted' on
    any failure.
    """
    if not raw_prefix or not raw_prefix.startswith("eyJ"):
        return raw_prefix  # not a token prefix — leave unchanged
    try:
        # Re-pad and decode
        padded = raw_prefix + "=" * (-len(raw_prefix) % 4)
        decoded = base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
        m = re.search(r'"agent_id"\s*:\s*"([^"]+)"', decoded)
        if m:
            return m.group(1)
    except Exception as exc:
        log.debug("decode_failed prefix=%s err=%s", raw_prefix[:8], exc)
    return "legacy_redacted"


async def _patch_table(
    table: str,
    column: str,
    url: str,
    key: str,
) -> dict:
    """
    Find all rows in `table` where `column` starts with 'eyJ', decode each,
    and PATCH the rows.  Returns summary stats.
    """
    import httpx

    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }

    # SELECT rows with eyJ-prefixed values using PostgREST LIKE filter
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"{url}/rest/v1/{table}",
            headers=headers,
            params={column: f"like.eyJ%", "limit": 10000, "select": f"id,{column}"},
        )
    if resp.status_code != 200:
        log.error("select_failed table=%s status=%s body=%s",
                  table, resp.status_code, resp.text[:300])
        return {"updated": 0, "failed": 0}

    rows = resp.json() or []
    log.info("found %d eyJ-prefixed rows in %s.%s", len(rows), table, column)
    if not rows:
        return {"updated": 0, "failed": 0}

    # Group rows by raw prefix value to batch PATCH by value
    by_prefix: dict[str, list[int]] = {}
    for row in rows:
        val = row.get(column, "")
        if val:
            by_prefix.setdefault(val, []).append(row.get("id"))

    updated = 0
    failed = 0

    async with httpx.AsyncClient(timeout=30.0) as client:
        for raw_prefix, ids in by_prefix.items():
            agent_id = _decode_agent_id(raw_prefix)
            log.info("patching %s.%s: %s -> %s (rows: %d)",
                     table, column, raw_prefix[:12] + "...", agent_id, len(ids))
            # PATCH using PostgREST filter on the old value
            patch_resp = await client.patch(
                f"{url}/rest/v1/{table}",
                headers={**headers, "Prefer": "return=minimal"},
                params={column: f"eq.{raw_prefix}"},
                json={column: agent_id},
            )
            if patch_resp.status_code in (200, 204):
                updated += len(ids)
            else:
                log.error("patch_failed table=%s status=%s body=%s",
                          table, patch_resp.status_code, patch_resp.text[:200])
                failed += len(ids)

    return {"updated": updated, "failed": failed}


async def _verify_clean(table: str, column: str, url: str, key: str) -> int:
    """Return the count of remaining eyJ-prefixed rows (should be 0)."""
    import httpx
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{url}/rest/v1/{table}",
            headers={"apikey": key, "Authorization": f"Bearer {key}"},
            params={column: "like.eyJ%", "limit": 1, "select": column},
        )
    if resp.status_code == 200:
        return len(resp.json() or [])
    return -1  # error


async def main() -> int:
    url = os.getenv("SUPABASE_URL", "").rstrip("/")
    key = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    if not url or not key:
        log.error("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set")
        return 1

    log.info("=== Credential hygiene backfill starting ===")
    log.info("target: %s", url)

    # Patch usage_events.key_id
    r1 = await _patch_table("usage_events", "key_id", url, key)
    log.info("usage_events: updated=%d failed=%d", r1["updated"], r1["failed"])

    # Patch operations.agent_id
    r2 = await _patch_table("operations", "agent_id", url, key)
    log.info("operations: updated=%d failed=%d", r2["updated"], r2["failed"])

    # Verify
    v1 = await _verify_clean("usage_events", "key_id", url, key)
    v2 = await _verify_clean("operations", "agent_id", url, key)
    log.info("verification: usage_events remaining_eyj=%s operations remaining_eyj=%s", v1, v2)

    if v1 == 0 and v2 == 0:
        log.info("=== VERIFIED: zero raw-token prefixes remain in both tables ===")
        return 0
    else:
        log.error("=== BACKFILL INCOMPLETE: eyJ rows remain (usage_events=%s, operations=%s) ===",
                  v1, v2)
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
