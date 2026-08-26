"""
Sender-number pool.

THE PROBLEM this closes. WhatsApp replies carry no addressing beyond the number
pair. If two end-users both have a live thread with the same business on the
SAME sender number, a bare "yes" from that business is genuinely unattributable
— conversations.py flags it as `pair_conflict` and we fall back to demanding a
reference token in the message. That works, but it pushes our problem onto the
business, and a business that has to quote reference numbers stops replying.

The structural fix is to not create the collision: put the second end-user on a
DIFFERENT sender number, so the pair is unique again and a bare "yes" is
unambiguous. That is what a pool is for.

DEGRADES CLEANLY. With one configured number the pool is a no-op and behaviour
is exactly what it is today (contested thread -> firm reference line). Adding a
number is pure configuration — no code change — which is the point: it turns a
scaling limit into something the founder can lift in one step.

  WHATSAPP_NUMBER_POOL="15556677792:1329399510252420,15551234567:9876543210"
                        ^display number ^phone_number_id

Falls back to WHATSAPP_PHONE_NUMBER / WHATSAPP_PHONE_ID when unset.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("smb_broker.number_pool")


@dataclass(frozen=True)
class Sender:
    number: str      # digits only
    phone_id: str

    def as_metadata(self) -> dict:
        return {"whatsapp_phone_id": self.phone_id,
                "whatsapp_from": self.number}


def _digits(v: str) -> str:
    return "".join(c for c in (v or "") if c.isdigit())


def load_pool() -> list[Sender]:
    """Parse the pool from env. Never raises; a malformed entry is skipped."""
    raw = os.getenv("WHATSAPP_NUMBER_POOL", "").strip()
    senders: list[Sender] = []
    if raw:
        for chunk in raw.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            number, _, phone_id = chunk.partition(":")
            number, phone_id = _digits(number), phone_id.strip()
            if number and phone_id:
                senders.append(Sender(number, phone_id))
            else:
                logger.warning("number_pool_bad_entry entry=%r", chunk[:40])
    # The single configured number is always a member — a pool that silently
    # excluded the number we actually send from would break every send.
    solo = Sender(_digits(os.getenv("WHATSAPP_PHONE_NUMBER", "")),
                  os.getenv("WHATSAPP_PHONE_ID", "").strip())
    if solo.number and solo.phone_id and not any(s.number == solo.number for s in senders):
        senders.insert(0, solo)
    return senders


@dataclass
class Allocation:
    sender: Optional[Sender]
    contested: bool          # True = every number already has a live thread here
    live_threads: int        # on the CHOSEN number, for this business


async def allocate(business_number: str) -> Allocation:
    """Pick the sender number with no live thread to this business.

    Preference order: a number with ZERO live threads (collision avoided
    entirely), else the number carrying the fewest (so the reference-token
    fallback is needed as rarely as possible, and load spreads evenly).

    FAIL-OPEN: if the ledger cannot be read we return the first sender and mark
    it contested. Refusing to send because we could not count threads would turn
    a bookkeeping problem into an outage — but claiming "uncontested" without
    evidence would be a lie, so we say contested and the firm reference line
    goes out. Degrading to the safe-but-noisier path is the honest default.
    """
    pool = load_pool()
    if not pool:
        return Allocation(sender=None, contested=False, live_threads=0)
    if len(pool) == 1:
        # No alternative exists, so counting cannot change the outcome — but the
        # caller still needs to know whether the thread is contested.
        n = await _live_count(pool[0].number, business_number)
        return Allocation(pool[0], contested=n > 0, live_threads=n)

    counts: list[tuple[int, int, Sender]] = []
    for idx, s in enumerate(pool):
        try:
            n = await _live_count(s.number, business_number)
        except Exception as exc:  # noqa: BLE001
            logger.warning("number_pool_count_failed number=%s err=%s", s.number[-4:], exc)
            return Allocation(pool[0], contested=True, live_threads=-1)
        if n == 0:
            return Allocation(s, contested=False, live_threads=0)
        # idx keeps the sort stable, so allocation is deterministic and testable.
        counts.append((n, idx, s))

    counts.sort()
    fewest, _, chosen = counts[0]
    return Allocation(chosen, contested=True, live_threads=fewest)


async def _live_count(our_number: str, business_number: str) -> int:
    from core.conversations import live_threads_for_pair
    threads = await live_threads_for_pair(our_number, business_number, limit=25)
    return len(threads or [])
