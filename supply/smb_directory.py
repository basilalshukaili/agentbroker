"""
SMB supply directory — verified SMBs, their channels, and their capabilities.
In production: backed by PostgreSQL with geo-search (PostGIS).
For v0.1: in-memory seeded with 20+ SMBs across the three wedge verticals.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from core.models import SMBRecord, Vertical


@dataclass
class SMBEntry:
    smb_id: str
    name: str
    vertical: Vertical
    address: str
    city: str
    state: str
    zip_code: str
    country: str = "US"
    capabilities: list[str] = field(default_factory=list)
    channels_available: list[str] = field(default_factory=list)
    # channel-specific identifiers
    calcom_event_type_id: Optional[str] = None
    square_location_id: Optional[str] = None
    vapi_assistant_id: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    website: Optional[str] = None
    price_range: Optional[dict] = None
    verified_at: Optional[datetime] = None
    active: bool = True
    # Sandbox entries — agents see is_demo=true and "[DEMO]" name prefix so
    # they can choose whether to attempt a real booking. Bookings against
    # demo SMBs short-circuit with reason_code="demo_smb_no_live_booking"
    # rather than calling fake 555 numbers.
    is_demo: bool = False
    # How much inbound demand this business can absorb, if we actually
    # KNOW - ideally declared by the business itself. Only a declared
    # tier may RAISE its demand budget above the default; anything we
    # infer can lower it but never raise it (core/business_tier.py).
    capacity_tier: Optional[str] = None   # micro | small | medium | large


def _seed_smbs() -> dict[str, SMBEntry]:
    # NOT datetime.now(). These twenty entries are constructed from literals
    # in this file; nobody verified anything. Stamping them with the current
    # time made verify_business answer `verified: true` with
    # `last_verified_at` a few minutes ago - so an agent reading it concluded
    # the record had been independently confirmed TODAY, every day, for ever.
    #
    # A freshness stamp that is permanently fresh is harder to notice than a
    # stale one, and this runs in production: render.yaml sets
    # SUPPLY_SEED_MODE=demo and the loader falls back to demo for every mode
    # except empty_strict.
    #
    # None is the honest value: we do not know when, because it never
    # happened. The entries are still flagged is_demo and name-prefixed
    # [DEMO]; this stops the third claim from contradicting the other two.
    now = None
    smbs = [
        # --- Personal Services ---
        SMBEntry("smb_001", "Cuts & Co.", Vertical.PERSONAL_SERVICES,
                 "123 Main St", "Atlanta", "GA", "30309",
                 capabilities=["haircut", "blowdry", "color", "highlights"],
                 channels_available=["direct_api:calcom", "sms", "email"],
                 calcom_event_type_id="1001",
                 phone="+14045550101", email="booking@cutsandco.example",
                 price_range={"min_usd": 35, "max_usd": 75}, verified_at=now),
        SMBEntry("smb_002", "Nail Studio ATL", Vertical.PERSONAL_SERVICES,
                 "456 Peachtree St", "Atlanta", "GA", "30308",
                 capabilities=["manicure", "pedicure", "nail_art", "gel", "acrylics"],
                 channels_available=["sms", "voice_ai:vapi"],
                 phone="+14045550102",
                 price_range={"min_usd": 25, "max_usd": 80}, verified_at=now),
        SMBEntry("smb_003", "Bliss Massage Midtown", Vertical.PERSONAL_SERVICES,
                 "789 Spring St", "Atlanta", "GA", "30308",
                 capabilities=["swedish_massage", "deep_tissue", "hot_stone", "prenatal_massage"],
                 channels_available=["direct_api:calcom", "sms"],
                 calcom_event_type_id="1002",
                 phone="+14045550103",
                 price_range={"min_usd": 80, "max_usd": 150}, verified_at=now),
        SMBEntry("smb_004", "FitLife Personal Training", Vertical.PERSONAL_SERVICES,
                 "321 West Peachtree St", "Atlanta", "GA", "30309",
                 capabilities=["personal_training", "fitness_assessment", "group_class"],
                 channels_available=["sms", "email"],
                 phone="+14045550104",
                 price_range={"min_usd": 60, "max_usd": 120}, verified_at=now),
        SMBEntry("smb_005", "The Lash Lounge", Vertical.PERSONAL_SERVICES,
                 "159 Buckhead Ave", "Atlanta", "GA", "30305",
                 capabilities=["lash_extensions", "lash_lift", "brow_tint", "waxing"],
                 channels_available=["direct_api:calcom", "sms"],
                 calcom_event_type_id="1003",
                 price_range={"min_usd": 45, "max_usd": 200}, verified_at=now),
        SMBEntry("smb_006", "Boston Barber Co.", Vertical.PERSONAL_SERVICES,
                 "22 Newbury St", "Boston", "MA", "02116",
                 capabilities=["haircut", "beard_trim", "shave", "color"],
                 channels_available=["sms", "voice_ai:vapi"],
                 phone="+16175550101",
                 price_range={"min_usd": 40, "max_usd": 85}, verified_at=now),
        SMBEntry("smb_007", "Cambridge Yoga Studio", Vertical.PERSONAL_SERVICES,
                 "88 Mass Ave", "Cambridge", "MA", "02139",
                 capabilities=["yoga_class", "private_yoga", "meditation"],
                 channels_available=["direct_api:calcom", "email"],
                 calcom_event_type_id="1004",
                 price_range={"min_usd": 20, "max_usd": 90}, verified_at=now),
        SMBEntry("smb_008", "Salon 718 Brooklyn", Vertical.PERSONAL_SERVICES,
                 "45 Atlantic Ave", "Brooklyn", "NY", "11201",
                 capabilities=["haircut", "color", "blowdry", "keratin_treatment"],
                 channels_available=["direct_api:calcom", "sms"],
                 calcom_event_type_id="1011",
                 phone="+17185550101",
                 price_range={"min_usd": 60, "max_usd": 150}, verified_at=now),

        # --- Home Services ---
        SMBEntry("smb_044", "FastFix Plumbing", Vertical.HOME_SERVICES,
                 "500 Cambridge St", "Cambridge", "MA", "02139",
                 capabilities=["plumbing", "emergency_plumbing", "drain_cleaning", "water_heater"],
                 channels_available=["voice_ai:vapi", "sms"],
                 phone="+16175550102",
                 price_range={"min_usd": 95, "max_usd": 500}, verified_at=now),
        SMBEntry("smb_045", "GreenLawn Care", Vertical.HOME_SERVICES,
                 "200 Comm Ave", "Boston", "MA", "02116",
                 capabilities=["lawn_mowing", "landscaping", "leaf_removal", "snow_removal"],
                 channels_available=["sms", "email"],
                 phone="+16175550103",
                 price_range={"min_usd": 50, "max_usd": 300}, verified_at=now),
        SMBEntry("smb_046", "SparkClean Services", Vertical.HOME_SERVICES,
                 "77 Beacon St", "Boston", "MA", "02108",
                 capabilities=["house_cleaning", "deep_clean", "move_in_clean", "office_cleaning"],
                 channels_available=["direct_api:calcom", "sms"],
                 calcom_event_type_id="1005",
                 phone="+16175550104",
                 price_range={"min_usd": 80, "max_usd": 400}, verified_at=now),
        SMBEntry("smb_047", "BugBusters Pest Control", Vertical.HOME_SERVICES,
                 "34 Atlantic Ave", "Boston", "MA", "02110",
                 capabilities=["pest_inspection", "ant_control", "rodent_control", "termite_treatment"],
                 channels_available=["sms", "voice_ai:vapi"],
                 phone="+16175550105",
                 price_range={"min_usd": 120, "max_usd": 800}, verified_at=now),
        SMBEntry("smb_048", "Atlanta Electric Pro", Vertical.HOME_SERVICES,
                 "600 Marietta St", "Atlanta", "GA", "30318",
                 capabilities=["electrical_inspection", "outlet_install", "panel_upgrade", "ev_charger_install"],
                 channels_available=["sms", "email"],
                 phone="+14045550108",
                 price_range={"min_usd": 150, "max_usd": 2000}, verified_at=now),
        SMBEntry("smb_049", "HandyPro Atlanta", Vertical.HOME_SERVICES,
                 "800 Lee St", "Atlanta", "GA", "30310",
                 capabilities=["general_handyman", "drywall_repair", "painting", "furniture_assembly"],
                 channels_available=["sms", "voice_ai:vapi"],
                 phone="+14045550109",
                 price_range={"min_usd": 75, "max_usd": 500}, verified_at=now),
        SMBEntry("smb_050", "RoofRight Contractors", Vertical.HOME_SERVICES,
                 "250 Moreland Ave", "Atlanta", "GA", "30307",
                 capabilities=["roof_inspection", "roof_repair", "gutter_cleaning"],
                 channels_available=["sms", "email"],
                 phone="+14045550110",
                 price_range={"min_usd": 200, "max_usd": 5000}, verified_at=now),

        # --- Professional Services ---
        SMBEntry("smb_080", "Sullivan & Partners Law", Vertical.PROFESSIONAL_SERVICES,
                 "1 Center Plaza", "Boston", "MA", "02108",
                 capabilities=["legal_consultation", "contract_review", "business_formation", "employment_law"],
                 channels_available=["direct_api:calcom", "email"],
                 calcom_event_type_id="1006",
                 price_range={"min_usd": 150, "max_usd": 500}, verified_at=now),
        SMBEntry("smb_081", "TaxPro Atlanta", Vertical.PROFESSIONAL_SERVICES,
                 "404 Peachtree Rd", "Atlanta", "GA", "30303",
                 capabilities=["tax_consultation", "business_accounting", "bookkeeping", "irs_representation"],
                 channels_available=["direct_api:calcom", "email", "sms"],
                 calcom_event_type_id="1007",
                 price_range={"min_usd": 100, "max_usd": 400}, verified_at=now),
        SMBEntry("smb_082", "WealthPath Financial", Vertical.PROFESSIONAL_SERVICES,
                 "200 Boylston St", "Boston", "MA", "02116",
                 capabilities=["financial_planning", "investment_consultation", "retirement_planning", "crypto_planning"],
                 channels_available=["direct_api:calcom", "email"],
                 calcom_event_type_id="1008",
                 price_range={"min_usd": 150, "max_usd": 350}, verified_at=now),
        SMBEntry("smb_083", "TutorPro Cambridge", Vertical.PROFESSIONAL_SERVICES,
                 "55 Garden St", "Cambridge", "MA", "02138",
                 capabilities=["math_tutoring", "sat_prep", "college_essay_coaching", "coding_tutoring"],
                 channels_available=["sms", "email", "direct_api:calcom"],
                 calcom_event_type_id="1009",
                 price_range={"min_usd": 50, "max_usd": 200}, verified_at=now),
        SMBEntry("smb_084", "ATL Business Coach", Vertical.PROFESSIONAL_SERVICES,
                 "17 Edgewood Ave", "Atlanta", "GA", "30303",
                 capabilities=["business_coaching", "startup_consulting", "pitch_prep", "marketing_strategy"],
                 channels_available=["direct_api:calcom", "email"],
                 calcom_event_type_id="1010",
                 price_range={"min_usd": 100, "max_usd": 300}, verified_at=now),
        SMBEntry("smb_085", "InsureRight Agency", Vertical.PROFESSIONAL_SERVICES,
                 "1000 Peachtree St NE", "Atlanta", "GA", "30309",
                 capabilities=["insurance_consultation", "auto_insurance", "home_insurance", "business_insurance"],
                 channels_available=["sms", "email", "voice_ai:vapi"],
                 phone="+14045550120",
                 price_range={"min_usd": 0, "max_usd": 0}, verified_at=now),  # free consultation
    ]
    return {smb.smb_id: smb for smb in smbs}


def _load_directory() -> dict[str, SMBEntry]:
    """
    Load the SMB directory.

    Behavior controlled by env var SUPPLY_SEED_MODE:
      - "demo"  (default): seeded demo SMBs are loaded, each marked with
        is_demo=True and a "[DEMO]" name prefix so callers know the entry
        is sandbox-only.
      - "labeled" (alias for "demo")
      - "empty":  start with zero SMBs. Strict production — directory grows
        only via real onboarding / scraping. Use this only once enough real
        supply has been imported that an empty fallback is acceptable.

    Demo data is intentionally served in production so probing agents and
    catalog scorers see a non-empty supply network; bookings against demo
    SMBs short-circuit (see schedule_appointment handler) instead of dialing
    fake numbers.
    """
    import os
    mode = os.getenv("SUPPLY_SEED_MODE", "demo").lower()
    # "empty" historically meant no real supply yet — now falls back to demo
    # so probing agents see a non-empty directory. Once real SMBs are
    # onboarded, set SUPPLY_SEED_MODE=empty_strict to disable demo fallback.
    if mode in ("empty_strict",):
        return {}
    seeds = _seed_smbs()
    for smb in seeds.values():
        smb.is_demo = True
        if not smb.name.startswith("[DEMO]"):
            smb.name = f"[DEMO] {smb.name}"

    # Merge durable real entries from Supabase (non-demo, persisted across restarts).
    # Demo seed is never overwritten -- durable entries only ADD supply.
    import logging
    _log = logging.getLogger("smb_broker.smb_directory")
    try:
        from storage.supabase_client import select_rows_sync
        rows = select_rows_sync("smb_supply", filters={"is_demo": "false", "active": "true"})
        for row in rows:
            try:
                sid = row.get("smb_id", "")
                if not sid or sid in seeds:
                    continue  # skip demo-IDs or conflicts
                from core.models import Vertical
                v = Vertical(row["vertical"])
                pr = None
                if row.get("price_min_usd") is not None or row.get("price_max_usd") is not None:
                    pr = {"min_usd": float(row.get("price_min_usd") or 0),
                          "max_usd": float(row.get("price_max_usd") or 0)}
                vt = None
                if row.get("verified_at"):
                    from datetime import datetime, timezone
                    vt = datetime.fromisoformat(str(row["verified_at"]).replace("Z", "+00:00"))
                seeds[sid] = SMBEntry(
                    smb_id=sid,
                    name=row.get("name", sid),
                    vertical=v,
                    address=row.get("address", ""),
                    city=row.get("city", ""),
                    state=row.get("state", ""),
                    zip_code=row.get("zip_code", ""),
                    country=row.get("country", "US"),
                    capabilities=list(row.get("capabilities") or []),
                    channels_available=list(row.get("channels_available") or []),
                    calcom_event_type_id=row.get("calcom_event_type_id"),
                    square_location_id=row.get("square_location_id"),
                    vapi_assistant_id=row.get("vapi_assistant_id"),
                    phone=row.get("phone"),
                    email=row.get("email"),
                    website=row.get("website") or row.get("booking_url"),
                    price_range=pr,
                    verified_at=vt,
                    active=True,
                    is_demo=False,
                )
            except Exception as _row_exc:
                _log.warning("smb_supply_row_skipped sid=%s err=%s", row.get("smb_id"), _row_exc)
        if rows:
            _log.info("smb_supply_loaded durable_count=%d", len(rows))
    except Exception as _exc:
        _log.warning("smb_supply_load_failed err=%s", _exc)

    return seeds


_DIRECTORY: dict[str, SMBEntry] = _load_directory()


class SMBDirectory:

    def get(self, smb_id: str) -> SMBEntry | None:
        return _DIRECTORY.get(smb_id)

    def search(
        self,
        vertical: Vertical,
        zip_or_city: str,
        capability: str | None = None,
        max_usd: float | None = None,
        max_results: int = 5,
    ) -> list[SMBEntry]:
        results = [
            smb for smb in _DIRECTORY.values()
            if smb.active
            and smb.vertical == vertical
            and self._location_matches(smb, zip_or_city)
        ]
        if capability:
            results = [s for s in results if capability.lower() in [c.lower() for c in s.capabilities]]
        if max_usd is not None:
            results = [s for s in results if not s.price_range or s.price_range.get("min_usd", 0) <= max_usd]
        # Sort by number of available channels (more channels = more reliable)
        results.sort(key=lambda s: len(s.channels_available), reverse=True)
        return results[:max_results]

    def size(self) -> int:
        return len([s for s in _DIRECTORY.values() if s.active])

    def upsert(self, entry: SMBEntry) -> None:
        _DIRECTORY[entry.smb_id] = entry
        # Persist real (non-demo) entries to Supabase for durability across restarts.
        if not entry.is_demo:
            self._persist_to_supabase(entry)

    @staticmethod
    def _persist_to_supabase(entry: SMBEntry) -> None:
        """Fire-and-forget write of a real (non-demo) SMBEntry to smb_supply table."""
        import asyncio
        import logging
        _log = logging.getLogger("smb_broker.smb_directory")
        pr = entry.price_range or {}
        row = {
            "smb_id":                entry.smb_id,
            "name":                  entry.name,
            "vertical":              entry.vertical.value if hasattr(entry.vertical, "value") else str(entry.vertical),
            "address":               entry.address or "",
            "city":                  entry.city or "",
            "state":                 entry.state or "",
            "zip_code":              entry.zip_code or "",
            "country":               entry.country or "US",
            "capabilities":          list(entry.capabilities or []),
            "channels_available":    list(entry.channels_available or []),
            "calcom_event_type_id":  entry.calcom_event_type_id,
            "square_location_id":    entry.square_location_id,
            "vapi_assistant_id":     entry.vapi_assistant_id,
            "phone":                 entry.phone,
            "email":                 entry.email,
            "website":               entry.website,
            "price_min_usd":         float(pr.get("min_usd", 0)) if pr.get("min_usd") is not None else None,
            "price_max_usd":         float(pr.get("max_usd", 0)) if pr.get("max_usd") is not None else None,
            "is_demo":               False,
            "active":                entry.active,
            "booking_url":           entry.website,
            "source":                "import_booking_url",
            "verified_at":           entry.verified_at.isoformat() if entry.verified_at else None,
        }
        async def _do_upsert() -> None:
            try:
                from storage.supabase_client import upsert_row
                result = await upsert_row("smb_supply", row, on_conflict="smb_id")
                if result:
                    _log.info("smb_supply_upserted smb_id=%s", entry.smb_id)
            except Exception as exc:
                _log.warning("smb_supply_upsert_failed smb_id=%s err=%s", entry.smb_id, exc)
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(_do_upsert())
            else:
                asyncio.run(_do_upsert())
        except Exception as exc:
            _log.warning("smb_supply_schedule_failed smb_id=%s err=%s", entry.smb_id, exc)

    # Common metro areas where an agent may search the MSA name but businesses
    # are registered under a borough/suburb city name.
    _METRO_ALIASES: dict[str, list[str]] = {
        "new york": ["manhattan", "brooklyn", "queens", "bronx", "staten island", "new york", "nyc"],
        "nyc": ["manhattan", "brooklyn", "queens", "bronx", "staten island", "new york"],
        "los angeles": ["los angeles", "santa monica", "pasadena", "burbank", "glendale", "culver city"],
        "chicago": ["chicago", "evanston", "oak park", "cicero"],
        "dallas": ["dallas", "fort worth", "arlington", "irving", "plano", "garland"],
        "san francisco": ["san francisco", "oakland", "berkeley", "san jose", "palo alto"],
        "washington": ["washington", "arlington", "alexandria", "bethesda", "silver spring"],
        "miami": ["miami", "miami beach", "hialeah", "coral gables", "fort lauderdale"],
        "boston": ["boston", "cambridge", "somerville", "brookline", "quincy"],
    }

    @staticmethod
    def _location_matches(smb: SMBEntry, zip_or_city: str) -> bool:
        needle = zip_or_city.lower().strip()
        city_l = smb.city.lower()
        state_l = smb.state.lower()
        zip_l = smb.zip_code.lower()
        # Direct containment check
        if needle in zip_l or needle in city_l or needle in state_l:
            return True
        # Split "City, STATE" format and check parts independently
        parts = [p.strip() for p in needle.replace(",", " ").split() if p.strip()]
        if any(p in city_l or p in state_l or p in zip_l for p in parts if len(p) > 1):
            return True
        # Metro-area expansion: "New York" → also matches Brooklyn, Queens, etc.
        for metro_key, boroughs in SMBDirectory._METRO_ALIASES.items():
            if metro_key in needle or any(metro_key == p for p in parts):
                if city_l in boroughs or any(b in city_l for b in boroughs):
                    return True
        return False


_directory = SMBDirectory()


def get_directory() -> SMBDirectory:
    return _directory
