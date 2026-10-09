"""The one answer for an `smb_id` that is not in the supply network.

verify_business, capture_lead and schedule_appointment each used to answer an unknown id with
`reason_code="supply_unreachable"`. That is the published OUTAGE code (api/errors.md: server_error, retriable,
"the target SMB could not be reached via any available channel"), and reliability/retry_policy.py retries it. The
truth was "this id is not one of ours": a caller mistake, not retriable, and exactly what the already-published
`out_of_supply_network` (client_error) says. In the 14 days to 2026-10-09 verify_business answered 30 outside calls
that way (docs/reviews/2026-10-09-agentbroker-request-analysis.md).

The commonest way to meet it is the flow our own find_business notice describes: most find_business results are
OpenStreetMap listings whose `osm:` ids no verify or write tool can use. That case gets its own sentence, because the
remedy differs (contact the business; there is nothing to correct in the id).

Pure functions. The caller's id is stranger text: it is reduced to a short, markup-free form before it goes back.
"""
from __future__ import annotations

import re
from typing import Optional

from core.models import ErrorCode

REASON_CODE = ErrorCode.OUT_OF_SUPPLY_NETWORK.value

_NOT_SAFE = re.compile(r"[^A-Za-z0-9:/_.\-]")
_SHOWN_MAX = 48
_NOTHING_RAN = "Nothing was contacted, held or charged."


def _shown(smb_id: object) -> str:
    text = _NOT_SAFE.sub("", str(smb_id))[:_SHOWN_MAX]
    return text or "(blank)"


def is_openstreetmap_id(smb_id: object) -> bool:
    """find_business returns community-mapped OpenStreetMap listings with ids of the form `osm:node/123`."""
    return isinstance(smb_id, str) and smb_id.startswith("osm:")


def not_in_network(smb_id: object, *, purpose: str, inactive: bool = False) -> tuple:
    """(human_message, next_actions) for an id the supply network does not hold.

    `purpose` completes "it cannot ...": "be verified", "be booked", "receive a lead". `inactive=True` is for an id the
    directory does hold but has switched off."""
    shown = _shown(smb_id)
    if is_openstreetmap_id(smb_id):
        message = (f"smb_id '{shown}' is an OpenStreetMap listing from find_business, not an entry in the "
                   f"AgentBroker supply network, so it cannot {purpose}. {_NOTHING_RAN}")
        actions = [
            "Contact the business through the phone or website on its find_business result; "
            "OpenStreetMap listings cannot be verified or booked here",
            "Run find_business again and use the smb_id of a record whose source is supply_network, "
            "if the result has one",
        ]
        return message, actions
    if inactive:
        message = (f"smb_id '{shown}' is in the supply network directory but is not active, so it cannot "
                   f"{purpose}. Take an smb_id from a current find_business result. {_NOTHING_RAN}")
        return message, ["Run find_business and use the smb_id of a record whose source is supply_network"]
    message = (f"smb_id '{shown}' is not in the AgentBroker supply network, so it cannot {purpose}. A usable "
               f"smb_id comes from a find_business result whose source is supply_network. {_NOTHING_RAN}")
    actions = [
        "Run find_business and use the smb_id of a record whose source is supply_network",
        "If you have the business's own booking page, import_booking_url adds it to the directory "
        "(needs a free key)",
    ]
    return message, actions
