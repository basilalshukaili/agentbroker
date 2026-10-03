"""Tell the sign-in which credit account an email bought.

The Polar order webhook is the only place that sees a buyer's email AND the customer id the credits are
granted to (`sub_<polar customer id>`). It calls `link_purchase` right after a successful grant; the sign-in's
account lookup (`oauth_account_for_email`) reads the link when it mints a token, so credits bought on the
website follow the person into the assistant at its next refresh.

Best-effort and silent on failure by design: the order is already fulfilled, a missing link only means the
person's assistant keeps the free identity until the link is written, and nothing here may ever make a paid
order fail. First writer wins in the database, so a retried webhook is harmless and no caller can re-point
an existing link.
"""
from __future__ import annotations

import logging
from typing import Optional

from agent_interface.oauth import tokens

log = logging.getLogger("smb_broker.oauth")


async def link_purchase(email: Optional[str], account_id: str, customer_id: Optional[str], plan: Optional[str]) -> bool:
    """True when a new link was written. Never raises."""
    try:
        if not email or "@" not in email or not str(account_id).startswith("sub_"):
            return False
        from agent_interface.oauth.store import get_store
        return bool(await get_store().account_link(tokens.email_hash(email), account_id, customer_id, plan))
    except Exception as exc:  # noqa: BLE001
        log.warning("oauth_account_link_failed err=%s", type(exc).__name__)
        return False
