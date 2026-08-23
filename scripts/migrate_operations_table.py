"""
One-time migration: create the `operations` table in Supabase.
Run locally with: python scripts/migrate_operations_table.py
Safe to re-run (CREATE TABLE IF NOT EXISTS).
"""
from __future__ import annotations

import asyncio
import os
import sys

# Allow running from repo root without install
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


_SQL = """
CREATE TABLE IF NOT EXISTS operations (
    operation_id  TEXT        PRIMARY KEY,
    ts            TIMESTAMPTZ NOT NULL DEFAULT now(),
    tool          TEXT        NOT NULL,
    status        TEXT        NOT NULL,
    reason_code   TEXT,
    result_json   JSONB,
    agent_id      TEXT
);

-- Speed up the two most common reads (get_status / get_outcome by operation_id)
-- operation_id is already the PK so the lookup is O(1); no extra index needed.
-- Optional: index agent_id for per-agent billing queries.
CREATE INDEX IF NOT EXISTS operations_agent_id_idx ON operations (agent_id)
    WHERE agent_id IS NOT NULL;
"""


async def run() -> None:
    db_url = os.getenv("SUPABASE_DB_URL") or os.getenv("DATABASE_URL")
    if not db_url:
        print("ERROR: SUPABASE_DB_URL not set — set it and retry.", file=sys.stderr)
        sys.exit(1)

    try:
        import asyncpg
    except ImportError:
        print("ERROR: asyncpg not installed — pip install asyncpg", file=sys.stderr)
        sys.exit(1)

    print(f"Connecting to {db_url[:40]}...")
    conn = await asyncpg.connect(db_url)
    try:
        await conn.execute(_SQL)
        print("OK: operations table created (or already exists).")
    finally:
        await conn.close()


if __name__ == "__main__":
    # Load .env from parent directory if present
    try:
        from pathlib import Path
        env_path = Path(__file__).parent.parent.parent / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip())
    except Exception:
        pass

    asyncio.run(run())
