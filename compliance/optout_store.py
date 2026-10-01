"""The durable opt-out list: load it at start, write a STOP to it.

WHY THIS FILE EXISTS (key-holder audit fix 5, 2026-10-01). Every touch of `consent_optouts` from
this container used the table directly, as the `anon` role. On Supabase that was refused by RLS
(an empty-but-200 read, a rejected write); on the spine it is refused outright (HTTP 403, no grant).
The consequences were all silent or half-silent:

  * start-up hydration failed with OPTOUT_HYDRATION_FAILED, so the in-memory opt-out set - the ONLY
    thing core/demand_queue.py, core/schedule_appointment.py and the WhatsApp webhook consult - began
    every process empty;
  * a STOP could not be recorded: handle_inbound answered `opt_out_not_recorded` forever, the
    unsubscribe page admitted the suppression was in-memory only, and the WhatsApp STOP was dropped.

Both now go through migrations/spine/009's SECURITY DEFINER functions (`consent_optouts_hydrate`,
`consent_optouts_record`). The per-send authoritative check is unchanged and elsewhere
(compliance/optout_gate.py -> consent_optouts_is_opted_out).

Failure handling keeps the codebase's rule: "I could not read/write it" is never the same answer as
"nothing to read" / "written". Everything here raises SupabaseUnavailable except the explicitly
lenient writer, which returns None and says so in the log.
"""
from __future__ import annotations

import logging
from typing import Optional

from storage.supabase_client import SupabaseUnavailable

logger = logging.getLogger("smb_broker.optout_store")

HYDRATE_PAGE = 1000          # the function clamps to 1000; asking for more only hides the cliff
HYDRATE_MAX_PAGES = 50       # 50,000 opt-outs; past that the caller logs OPTOUT_HYDRATION_TRUNCATED


async def load_durable_optouts() -> list:
    """Every (recipient_id, channel) on the durable opt-out list, paged and ordered.

    Raises SupabaseUnavailable when it could not be read - never returns a partial list as if it
    were whole, except at the documented page cap (the caller logs that loudly).
    """
    from storage.supabase_client import rpc

    pairs: list = []
    offset = 0
    for _ in range(HYDRATE_MAX_PAGES):
        try:
            rows = await rpc("consent_optouts_hydrate",
                             {"p_limit": HYDRATE_PAGE, "p_offset": offset})
        except Exception as exc:  # noqa: BLE001
            raise SupabaseUnavailable(f"consent_optouts_hydrate failed: {exc}") from exc
        if not isinstance(rows, list):
            raise SupabaseUnavailable(
                f"consent_optouts_hydrate returned {type(rows).__name__}, not a list - "
                "refusing to read it as 'nobody has opted out'")
        for row in rows:
            if isinstance(row, dict):
                pairs.append((row.get("recipient_id"), row.get("channel")))
        if len(rows) < HYDRATE_PAGE:
            return pairs
        offset += HYDRATE_PAGE
    return pairs                                   # page cap reached: caller sees len == cap


async def record_optout(
    recipient_id: str,
    channel: str,
    *,
    use_case: str = "marketing",
    revocation_method: Optional[str] = None,
    source: Optional[str] = None,
    created_at: Optional[str] = None,
) -> dict:
    """Write one opt-out durably. Idempotent on (recipient_id, channel). Raises SupabaseUnavailable
    when the write did not happen - the callers that promise the person "you are unsubscribed"
    must be able to tell."""
    from storage.supabase_client import rpc

    payload = {
        "p_recipient_id": recipient_id,
        "p_channel": channel,
        "p_use_case": use_case,
        "p_revocation_method": revocation_method,
        "p_source": source,
    }
    if created_at:
        payload["p_created_at"] = created_at
    try:
        out = await rpc("consent_optouts_record", payload)
    except Exception as exc:  # noqa: BLE001
        raise SupabaseUnavailable(f"consent_optouts_record failed: {exc}") from exc
    if not isinstance(out, dict) or out.get("recorded") is not True:
        raise SupabaseUnavailable("consent_optouts_record did not confirm the write")
    return out


async def record_optout_lenient(*args, **kwargs) -> Optional[dict]:
    """record_optout for callers whose own contract is 'returns None on failure' (handle_inbound,
    the WhatsApp webhook). Never raises; a failure is logged at ERROR with no recipient in it."""
    try:
        return await record_optout(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        logger.error("optout_durable_write_failed channel=%s err=%s",
                     kwargs.get("channel") or (args[1] if len(args) > 1 else "?"), exc)
        return None
