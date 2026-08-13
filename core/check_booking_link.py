"""
check_booking_link — read-only pre-flight classifier for booking URLs.

The problem it solves for a calling agent:
    Before spending money on a booking attempt (schedule_appointment is
    $0.15-0.50 and can take up to 60s), an agent wants to know CHEAPLY and
    INSTANTLY whether a URL a user pasted is actually a booking page this
    broker can act on, which platform it is, and how the booking will route.

This tool answers exactly that WITHOUT any network call and WITHOUT mutating
state. It is the free/cheap top-of-funnel read that de-risks the paid
import_booking_url -> schedule_appointment path:

    check_booking_link(url)            # <- this tool: classify, $0, instant
      -> import_booking_url(url)       # turns URL into a callable smb_id
      -> schedule_appointment(smb_id)  # the paid action

Design notes / honesty guarantees:
  * NO network I/O. This is pure classification (URL shape + platform regex +
    supported-host allowlist). It therefore does NOT claim "the page is live"
    — it never fetched it. Overclaiming liveness we didn't verify would be the
    same class of lie as the old stub-SUCCESS bug. We only report what we can
    prove offline: is this a supported booking platform, and if so how does it
    route.
  * The `importable` verdict mirrors exactly the gate import_booking_url
    applies (its host allowlist), so a `true` here means import_booking_url
    will accept the URL — no surprises one call later.
  * A "not a supported platform" URL is a SUCCESSFUL check (the tool did its
    job), not a failure — same convention as find_business returning an empty
    result set. Only a malformed URL is a FAILURE(bad_input).
"""
from __future__ import annotations

import time
import uuid
from urllib.parse import urlparse

from core.models import CostRecord, OperationStatus, OutcomeReceipt
from supply.booking_page_importer import (
    BookingPlatform,
    _PLATFORM_CHANNELS,
    _host_in_allowlist,
    _smb_id_for_url,
    default_capabilities_for,
    detect_platform,
    _infer_country_from_url,
)

_SUPPORTED_PLATFORMS_HUMAN = (
    "Cal.com, Calendly, Doctolib, Booksy, Fresha, OpenTable, Setmore, Square, "
    "Acuity, Schedulista, Squarespace, BookMyCity"
)


def _extract_handle(url: str) -> str | None:
    """Best-effort business handle/slug from the URL path (informational)."""
    try:
        path = urlparse(url).path or ""
    except (ValueError, TypeError):
        return None
    slug = path.strip("/")
    return slug or None


async def handle_check_booking_link(
    url: str,
    agent_id: str | None = None,
    trace_id: str | None = None,
) -> OutcomeReceipt:
    t0 = time.monotonic()

    # --- input validation -------------------------------------------------
    if not url or not isinstance(url, str) or not url.strip().startswith(("http://", "https://")):
        return OutcomeReceipt(
            operation_id=str(uuid.uuid4()),
            status=OperationStatus.FAILURE,
            reason_code="bad_input",
            human_message=(
                "url must be a full http(s) booking URL, e.g. "
                "'https://cal.com/jane' or 'https://www.opentable.com/r/acme'."
            ),
            cost=CostRecord(amount=0.0, currency="USD", basis="free"),
            latency_ms=int((time.monotonic() - t0) * 1000),
            retriable=False,
            trace_id=trace_id,
        )

    url = url.strip()
    host = (urlparse(url).hostname or "").lower()
    host_supported = _host_in_allowlist(host)

    # Only trust the platform label when the HOST itself is a supported
    # platform host. detect_platform matches the whole URL string, so a
    # decoy like https://evil.example/cal.com/x could otherwise mislabel;
    # gating on the host allowlist prevents that.
    platform = detect_platform(url) if host_supported else BookingPlatform.CUSTOM

    # --- supported / importable branch -----------------------------------
    if host_supported and platform is not BookingPlatform.CUSTOM:
        predicted_smb_id = _smb_id_for_url(url)
        result = {
            "supported": True,
            "importable": True,
            "platform": platform.value,
            "host": host,
            "handle": _extract_handle(url),
            "normalized_url": url,
            "predicted_smb_id": predicted_smb_id,
            "expected_channels": list(_PLATFORM_CHANNELS.get(platform, ["browser_automation"])),
            "expected_capabilities": default_capabilities_for(platform),
            "inferred_country": _infer_country_from_url(url),
            "verification": "offline_classification",
            "checked_live": False,
        }
        return OutcomeReceipt(
            operation_id=str(uuid.uuid4()),
            status=OperationStatus.SUCCESS,
            reason_code="supported_platform",
            human_message=(
                f"Recognized {platform.value} booking link. import_booking_url "
                f"will accept it and assign smb_id {predicted_smb_id}. Note: this "
                f"is an offline classification — the page itself was not fetched."
            ),
            result=result,
            cost=CostRecord(amount=0.0, currency="USD", basis="free"),
            latency_ms=int((time.monotonic() - t0) * 1000),
            retriable=False,
            trace_id=trace_id,
            next_actions=[
                f"Call import_booking_url(booking_url='{url}') to get the callable smb_id.",
                "Then call schedule_appointment(smb_id=..., action='book', requested_time=...).",
            ],
        )

    # --- unsupported branch (still a successful check) --------------------
    result = {
        "supported": False,
        "importable": False,
        "platform": "unknown",
        "host": host,
        "verification": "offline_classification",
        "checked_live": False,
        "reason": (
            "URL host is not one of the supported booking platforms, so "
            "import_booking_url would reject it."
        ),
        "supported_platforms": _SUPPORTED_PLATFORMS_HUMAN,
    }
    return OutcomeReceipt(
        operation_id=str(uuid.uuid4()),
        status=OperationStatus.SUCCESS,
        reason_code="unsupported_platform",
        human_message=(
            "Not a supported booking link. import_booking_url only accepts URLs on "
            f"these platforms: {_SUPPORTED_PLATFORMS_HUMAN}."
        ),
        result=result,
        cost=CostRecord(amount=0.0, currency="USD", basis="free"),
        latency_ms=int((time.monotonic() - t0) * 1000),
        retriable=False,
        trace_id=trace_id,
        next_actions=[
            "If the user only named a business + city, call find_business instead.",
            "If you have the business phone number, call_business can reach it directly.",
            "Ask the user for a URL on a supported platform if they have one.",
        ],
    )
