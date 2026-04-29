"""
PII retention policy enforcement.
Auto-expiry of personally identifiable information per jurisdiction.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from compliance.jurisdiction_rules import get_rules


def retention_expiry_for(
    jurisdiction_code: str,
    recorded_at: Optional[datetime] = None,
) -> datetime:
    """Return the datetime after which PII data must be deleted for this jurisdiction."""
    rules = get_rules(jurisdiction_code.split("-")[0],
                      jurisdiction_code.split("-")[1] if "-" in jurisdiction_code else None)
    base = recorded_at or datetime.now(timezone.utc)
    return base + timedelta(days=rules.pii_retention_days)


def is_expired(jurisdiction_code: str, recorded_at: datetime) -> bool:
    """Returns True if PII from this record should be deleted under the applicable rules."""
    expiry = retention_expiry_for(jurisdiction_code, recorded_at)
    return datetime.now(timezone.utc) > expiry


def gdpr_applies(country_code: str) -> bool:
    return get_rules(country_code).gdpr_applies


def casl_applies(country_code: str) -> bool:
    return get_rules(country_code).casl_applies
