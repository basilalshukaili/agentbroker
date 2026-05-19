"""
Public booking-page importer.

The strategic moat is *the metadata graph* — knowing which SMB uses which
booking system worldwide. This module ingests a public booking-page URL
and extracts an SMBEntry skeleton, classifying the underlying booking
platform so we know how to route bookings later.

Supported platforms (auto-detected by URL pattern + page content):

    cal.com           — Cal.com (open-source scheduler)
    calendly.com      — Calendly (US scheduler)
    doctolib.fr/.de   — Doctolib (EU healthcare)
    bookmycity.in     — BookMyCity (India)
    booksy.com        — Booksy (worldwide salons)
    fresha.com        — Fresha (worldwide salons)
    setmore.com       — Setmore
    square.site       — Square Appointments
    opentable.com     — OpenTable (restaurants)
    schedulista.com   — Schedulista
    acuity            — Acuity Scheduling
    squarespace       — Squarespace bookings
    custom            — generic / unknown — flagged for manual review

The importer is idempotent: re-running on the same URL returns the same SMB.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
from urllib.parse import urlparse

from core.models import OperationStatus, Vertical
from supply.smb_directory import SMBDirectory, SMBEntry


# ---------------------------------------------------------------------------
# Booking platform taxonomy
# ---------------------------------------------------------------------------

class BookingPlatform(str, Enum):
    CAL_COM = "cal.com"
    CALENDLY = "calendly"
    DOCTOLIB = "doctolib"
    BOOKMYCITY = "bookmycity"
    BOOKSY = "booksy"
    FRESHA = "fresha"
    SETMORE = "setmore"
    SQUARE = "square"
    OPENTABLE = "opentable"
    SCHEDULISTA = "schedulista"
    ACUITY = "acuity"
    SQUARESPACE = "squarespace"
    CUSTOM = "custom"


# Pattern map — order matters, more-specific first.
# `\b` matches word boundary so we catch `https://cal.com/x` AND `https://www.cal.com/x`.
_PLATFORM_PATTERNS: list[tuple[BookingPlatform, re.Pattern]] = [
    (BookingPlatform.CAL_COM,      re.compile(r"\bcal\.com/", re.I)),
    (BookingPlatform.CALENDLY,     re.compile(r"calendly\.com/", re.I)),
    (BookingPlatform.DOCTOLIB,     re.compile(r"doctolib\.(?:fr|de|it)/", re.I)),
    (BookingPlatform.BOOKMYCITY,   re.compile(r"bookmycity\.", re.I)),
    (BookingPlatform.BOOKSY,       re.compile(r"booksy\.com/", re.I)),
    (BookingPlatform.FRESHA,       re.compile(r"fresha\.com/", re.I)),
    (BookingPlatform.SETMORE,      re.compile(r"setmore\.com/", re.I)),
    (BookingPlatform.SQUARE,       re.compile(r"squareup\.com|square\.site", re.I)),
    (BookingPlatform.OPENTABLE,    re.compile(r"opentable\.com/", re.I)),
    (BookingPlatform.SCHEDULISTA,  re.compile(r"schedulista\.com/", re.I)),
    (BookingPlatform.ACUITY,       re.compile(r"acuityscheduling\.com|app\.acuityscheduling", re.I)),
    (BookingPlatform.SQUARESPACE,  re.compile(r"squarespace.com/.*scheduling", re.I)),
]


def detect_platform(url: str) -> BookingPlatform:
    for platform, pattern in _PLATFORM_PATTERNS:
        if pattern.search(url):
            return platform
    return BookingPlatform.CUSTOM


# ---------------------------------------------------------------------------
# Channel mapping per platform
# ---------------------------------------------------------------------------

_PLATFORM_CHANNELS: dict[BookingPlatform, list[str]] = {
    BookingPlatform.CAL_COM:     ["direct_api:calcom"],
    BookingPlatform.CALENDLY:    ["direct_api:calendly", "browser_automation"],
    BookingPlatform.DOCTOLIB:    ["browser_automation"],
    BookingPlatform.BOOKSY:      ["browser_automation"],
    BookingPlatform.FRESHA:      ["browser_automation"],
    BookingPlatform.SETMORE:     ["browser_automation"],
    BookingPlatform.SQUARE:      ["browser_automation"],
    BookingPlatform.OPENTABLE:   ["browser_automation"],
    BookingPlatform.SCHEDULISTA: ["browser_automation"],
    BookingPlatform.ACUITY:      ["direct_api:acuity", "browser_automation"],
    BookingPlatform.SQUARESPACE: ["browser_automation"],
    BookingPlatform.BOOKMYCITY:  ["browser_automation"],
    BookingPlatform.CUSTOM:      ["browser_automation"],
}


# ---------------------------------------------------------------------------
# Default capability seeds per platform
#
# Every imported booking URL gets the generic booking trio so verify_business
# answers "yes" to obvious questions like capability_to_verify="booking".
# Platform-specific extras give a richer signal (Doctolib -> medical, etc.).
# ---------------------------------------------------------------------------

_BASE_BOOKING_CAPABILITIES = ["booking", "appointment", "schedule"]

_PLATFORM_CAPABILITIES: dict[BookingPlatform, list[str]] = {
    BookingPlatform.CAL_COM:     ["consultation", "meeting"],
    BookingPlatform.CALENDLY:    ["consultation", "meeting"],
    BookingPlatform.DOCTOLIB:    ["medical_consultation", "doctor_visit"],
    BookingPlatform.BOOKSY:      ["salon", "haircut", "beauty"],
    BookingPlatform.FRESHA:      ["salon", "haircut", "beauty"],
    BookingPlatform.SETMORE:     [],
    BookingPlatform.SQUARE:      [],
    BookingPlatform.OPENTABLE:   ["reservation", "dining"],
    BookingPlatform.SCHEDULISTA: [],
    BookingPlatform.ACUITY:      ["consultation"],
    BookingPlatform.SQUARESPACE: [],
    BookingPlatform.BOOKMYCITY:  [],
    BookingPlatform.CUSTOM:      [],
}


def default_capabilities_for(platform: BookingPlatform) -> list[str]:
    """Capabilities to seed when an imported URL has none supplied by the caller."""
    extras = _PLATFORM_CAPABILITIES.get(platform, [])
    # Preserve order, dedupe.
    return list(dict.fromkeys(_BASE_BOOKING_CAPABILITIES + extras))


# ---------------------------------------------------------------------------
# Import request / result
# ---------------------------------------------------------------------------

@dataclass
class ImportRequest:
    booking_url: str
    business_name: Optional[str] = None
    vertical: Optional[Vertical] = None
    country_code: Optional[str] = None  # ISO-3166-1 alpha-2; auto-detect from URL TLD if not given
    contact_phone: Optional[str] = None
    contact_email: Optional[str] = None
    capabilities: list[str] = field(default_factory=list)


@dataclass
class ImportResult:
    status: OperationStatus
    smb_id: Optional[str] = None
    platform: Optional[BookingPlatform] = None
    message: str = ""
    next_steps: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# TLD → country code shortcut (best-effort)
# ---------------------------------------------------------------------------

_TLD_TO_COUNTRY = {
    ".uk": "GB", ".de": "DE", ".fr": "FR", ".it": "IT", ".es": "ES",
    ".nl": "NL", ".be": "BE", ".pl": "PL", ".se": "SE", ".no": "NO",
    ".dk": "DK", ".fi": "FI", ".ch": "CH", ".at": "AT", ".pt": "PT",
    ".ie": "IE", ".gr": "GR",
    ".jp": "JP", ".kr": "KR", ".cn": "CN", ".sg": "SG", ".hk": "HK",
    ".my": "MY", ".id": "ID", ".th": "TH", ".vn": "VN", ".ph": "PH",
    ".in": "IN", ".pk": "PK", ".bd": "BD", ".lk": "LK",
    ".au": "AU", ".nz": "NZ",
    ".ae": "AE", ".sa": "SA", ".om": "OM", ".qa": "QA", ".kw": "KW",
    ".bh": "BH", ".jo": "JO", ".lb": "LB", ".eg": "EG", ".tr": "TR",
    ".za": "ZA", ".ng": "NG", ".ke": "KE", ".ma": "MA",
    ".br": "BR", ".mx": "MX", ".ar": "AR", ".cl": "CL", ".co": "CO",
    ".ca": "CA", ".us": "US",
}


def _infer_country_from_url(url: str) -> Optional[str]:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    for tld, country in _TLD_TO_COUNTRY.items():
        if host.endswith(tld):
            return country
    return None


# ---------------------------------------------------------------------------
# SMB ID generator (idempotent)
# ---------------------------------------------------------------------------

def _smb_id_for_url(url: str) -> str:
    digest = hashlib.sha256(url.lower().strip().encode()).hexdigest()[:16]
    return f"smb_imp_{digest}"


# ---------------------------------------------------------------------------
# Main importer
# ---------------------------------------------------------------------------

async def import_from_booking_url(req: ImportRequest) -> ImportResult:
    """
    Idempotent ingest of a booking page. Returns the SMBEntry's smb_id.
    Optionally fetches the page to extract title / og:title for business name.
    """
    if not req.booking_url or not req.booking_url.startswith(("http://", "https://")):
        return ImportResult(
            status=OperationStatus.FAILURE,
            message="booking_url must be a full http(s) URL.",
        )

    platform = detect_platform(req.booking_url)
    smb_id = _smb_id_for_url(req.booking_url)

    directory = SMBDirectory()
    existing = directory.get(smb_id)
    if existing:
        return ImportResult(
            status=OperationStatus.SUCCESS,
            smb_id=smb_id,
            platform=platform,
            message=f"Already imported as {existing.name}.",
        )

    # Best-effort title extraction
    business_name = req.business_name or await _extract_title(req.booking_url) or req.booking_url

    country_code = (req.country_code or _infer_country_from_url(req.booking_url) or "INTERNATIONAL").upper()

    # If user didn't supply a vertical, default to PROFESSIONAL_SERVICES (safe generic)
    vertical = req.vertical or Vertical.PROFESSIONAL_SERVICES

    channels = list(_PLATFORM_CHANNELS.get(platform, ["browser_automation"]))
    if req.contact_phone:
        channels.append("voice_ai:vapi")
        channels.append("sms")
    if req.contact_email:
        channels.append("email")

    # Seed booking capabilities so verify_business answers "yes" to obvious
    # capability names ("booking", "appointment", "schedule") plus any
    # platform-implied extras (cal.com -> consultation, doctolib -> medical, etc.).
    # Caller-supplied capabilities take precedence and are merged on top.
    seeded_caps = default_capabilities_for(platform)
    capabilities = list(dict.fromkeys(seeded_caps + list(req.capabilities or [])))

    entry = SMBEntry(
        smb_id=smb_id,
        name=business_name,
        vertical=vertical,
        address="",
        city="",
        state="",
        zip_code="",
        country=country_code if country_code != "INTERNATIONAL" else "XX",
        capabilities=capabilities,
        channels_available=list(dict.fromkeys(channels)),  # preserve order, dedupe
        phone=req.contact_phone,
        email=req.contact_email,
        website=req.booking_url,
        # platform metadata stored in price_range slot for now (will move to dedicated field in v0.2)
        price_range={"booking_platform": platform.value, "imported_at": time.time()},
        verified_at=None,
        active=True,
    )
    directory.upsert(entry)

    next_steps = [
        f"Detected platform: {platform.value}.",
        "Add a contact phone or email to enable channel fallback.",
    ]
    if platform == BookingPlatform.CUSTOM:
        next_steps.append("Custom platform — booking will route through browser automation; expect higher latency.")

    return ImportResult(
        status=OperationStatus.SUCCESS,
        smb_id=smb_id,
        platform=platform,
        message=f"Imported '{business_name}' from {platform.value}.",
        next_steps=next_steps,
    )


async def _extract_title(url: str) -> Optional[str]:
    """Best-effort title extraction. Returns None on any failure (offline, blocked, etc.)."""
    try:
        import httpx
        async with httpx.AsyncClient(follow_redirects=True, timeout=5.0) as client:
            resp = await client.get(url, headers={"User-Agent": "smb-broker-importer/0.1"})
            if resp.status_code != 200:
                return None
            html = resp.text
            # og:title first, then <title>
            og_match = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', html, re.I)
            if og_match:
                return og_match.group(1).strip()[:120]
            title_match = re.search(r"<title>([^<]+)</title>", html, re.I)
            if title_match:
                return title_match.group(1).strip()[:120]
    except Exception:
        return None
    return None


# ---------------------------------------------------------------------------
# Bulk import — feed a list of URLs
# ---------------------------------------------------------------------------

async def bulk_import(urls: list[str]) -> list[ImportResult]:
    """Import many URLs in parallel. Useful for seeding or migration."""
    return await asyncio.gather(*[
        import_from_booking_url(ImportRequest(booking_url=url)) for url in urls
    ])
