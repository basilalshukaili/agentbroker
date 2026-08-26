"""
Pricing tiers -- RETIRED MODEL, NOT WIRED TO ANY LIVE BILLING PATH.

WARNING: nothing in production imports this module; only tests do. A test-only
    caller is still dead in production, which is exactly how it kept looking
    alive (audit 2026-08-26).

    THE LIVE PRICE TABLE IS `billing/pricing.py`. This module models a
    subscription shape ($49/mo Developer, $499/mo Business, uptime SLAs,
    refund-on-miss) that HatchLoop never sold and does not sell. It is kept
    because the revenue-stream thinking below is still worth something --
    outcome premiums, supply-side listing revenue, demand analytics -- not
    because any of it is in effect.

    Reviving any stream means wiring it THROUGH billing/pricing.py, so there
    stays exactly one price table. See docs/PRICING.md.

Five revenue streams (EXPLORED, NOT IN EFFECT):

  1. PAY-AS-YOU-GO (per-call) — base unit economics. Margin: ~40-60%.
  2. AGENT SUBSCRIPTIONS — predictable monthly revenue from frequent agents.
  3. OUTCOME-BASED PRICING — premium for confirmed bookings (highest margin).
  4. SMB-SIDE LISTING REVENUE — SMBs pay for premium placement.
  5. ANALYTICS RESALE — anonymized agent demand data sold back to SMBs.

The manifest's cost_model is generated from billing/pricing.py, NOT from
this module -- see scripts/sync_manifest_pricing.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Subscription tiers (agent-side)
# ---------------------------------------------------------------------------

class SubscriptionTier(str, Enum):
    FREE = "free"            # 100 ops/month, no SLA
    DEVELOPER = "developer"  # 10k ops/month, $49/mo, basic SLA
    BUSINESS = "business"    # 100k ops/month, $499/mo, 99.5% SLA, priority queue
    ENTERPRISE = "enterprise"  # custom volume, custom SLA, dedicated support


@dataclass
class TierConfig:
    tier: SubscriptionTier
    monthly_fee_usd: float
    included_ops: int
    overage_per_op_usd: float
    sla_uptime_pct: float
    sla_p50_latency_ms: int
    priority_queue: bool
    support_tier: str
    refund_policy: str


_TIERS: dict[SubscriptionTier, TierConfig] = {
    SubscriptionTier.FREE: TierConfig(
        tier=SubscriptionTier.FREE,
        monthly_fee_usd=0.0,
        included_ops=100,
        overage_per_op_usd=0.10,
        sla_uptime_pct=0.0,
        sla_p50_latency_ms=10000,
        priority_queue=False,
        support_tier="community_forum",
        refund_policy="none",
    ),
    SubscriptionTier.DEVELOPER: TierConfig(
        tier=SubscriptionTier.DEVELOPER,
        monthly_fee_usd=49.0,
        included_ops=10_000,
        overage_per_op_usd=0.04,
        sla_uptime_pct=99.0,
        sla_p50_latency_ms=5000,
        priority_queue=False,
        support_tier="email_24h",
        refund_policy="50% credit on SLA miss",
    ),
    SubscriptionTier.BUSINESS: TierConfig(
        tier=SubscriptionTier.BUSINESS,
        monthly_fee_usd=499.0,
        included_ops=100_000,
        overage_per_op_usd=0.025,
        sla_uptime_pct=99.5,
        sla_p50_latency_ms=2500,
        priority_queue=True,
        support_tier="email_4h",
        refund_policy="100% credit on SLA miss",
    ),
    SubscriptionTier.ENTERPRISE: TierConfig(
        tier=SubscriptionTier.ENTERPRISE,
        monthly_fee_usd=0.0,                 # negotiated
        included_ops=0,                      # negotiated
        overage_per_op_usd=0.015,
        sla_uptime_pct=99.9,
        sla_p50_latency_ms=2000,
        priority_queue=True,
        support_tier="dedicated_slack_1h",
        refund_policy="100% + revenue share guarantee",
    ),
}


def get_tier(tier: SubscriptionTier) -> TierConfig:
    return _TIERS[tier]


def list_tiers() -> list[TierConfig]:
    return list(_TIERS.values())


# ---------------------------------------------------------------------------
# Outcome-based premium pricing
# ---------------------------------------------------------------------------

@dataclass
class OutcomePremium:
    operation: str
    base_cost_usd: float       # what agent pays per call (regardless of outcome)
    success_premium_usd: float # additional fee on confirmed success
    description: str


_OUTCOME_PREMIUMS: dict[str, OutcomePremium] = {
    "schedule_appointment": OutcomePremium(
        operation="schedule_appointment",
        base_cost_usd=0.15,
        success_premium_usd=0.85,        # $1.00 total for confirmed booking
        description="Total $1.00 for a confirmed booking; $0.15 if booking fails. Agents prefer this — they only pay full price for value delivered.",
    ),
    "capture_lead": OutcomePremium(
        operation="capture_lead",
        base_cost_usd=0.02,
        success_premium_usd=0.18,        # $0.20 total for accepted lead
        description="Total $0.20 if SMB accepts the lead; $0.02 if rejected.",
    ),
    "send_message": OutcomePremium(
        operation="send_message",
        base_cost_usd=0.05,
        success_premium_usd=0.0,         # no outcome premium — delivery is the outcome
        description="Flat $0.05 per delivered message; no premium.",
    ),
    "escalate_to_human": OutcomePremium(
        operation="escalate_to_human",
        base_cost_usd=0.10,
        success_premium_usd=0.40,        # $0.50 if human resolves; $0.10 if abandoned
        description="$0.50 on human-confirmed resolution; $0.10 if escalation expires.",
    ),
}


def get_outcome_premium(operation: str) -> Optional[OutcomePremium]:
    return _OUTCOME_PREMIUMS.get(operation)


# ---------------------------------------------------------------------------
# SMB-side listing revenue (NEW revenue stream — defends against incumbents)
# ---------------------------------------------------------------------------

class ListingTier(str, Enum):
    FREE = "free"               # Default. Listed but not boosted.
    VERIFIED = "verified"       # $29/mo. Verified badge, ranks above free.
    FEATURED = "featured"       # $99/mo. Top-of-list for searches matching capabilities.
    EXCLUSIVE = "exclusive"     # $499/mo. Sole result for a vertical+zip pair (limit 1 per zip).


@dataclass
class ListingTierConfig:
    tier: ListingTier
    monthly_fee_usd: float
    rank_boost: float          # multiplier on default rank score
    badge: Optional[str]
    exclusivity: bool
    description: str


_LISTING_TIERS: dict[ListingTier, ListingTierConfig] = {
    ListingTier.FREE: ListingTierConfig(
        tier=ListingTier.FREE,
        monthly_fee_usd=0.0,
        rank_boost=1.0,
        badge=None,
        exclusivity=False,
        description="Listed in the directory at default rank.",
    ),
    ListingTier.VERIFIED: ListingTierConfig(
        tier=ListingTier.VERIFIED,
        monthly_fee_usd=29.0,
        rank_boost=1.5,
        badge="verified",
        exclusivity=False,
        description="Verified badge displayed to agents; 1.5x rank boost.",
    ),
    ListingTier.FEATURED: ListingTierConfig(
        tier=ListingTier.FEATURED,
        monthly_fee_usd=99.0,
        rank_boost=2.5,
        badge="featured",
        exclusivity=False,
        description="Top-of-list for matching searches; 2.5x rank boost.",
    ),
    ListingTier.EXCLUSIVE: ListingTierConfig(
        tier=ListingTier.EXCLUSIVE,
        monthly_fee_usd=499.0,
        rank_boost=10.0,
        badge="exclusive_partner",
        exclusivity=True,
        description="Sole result for the vertical+zip pair. Capped at 1 SMB per (vertical, zip).",
    ),
}


def get_listing_tier(tier: ListingTier) -> ListingTierConfig:
    return _LISTING_TIERS[tier]


def list_listing_tiers() -> list[ListingTierConfig]:
    return list(_LISTING_TIERS.values())


# ---------------------------------------------------------------------------
# Revenue forecast helpers (for pricing.md)
# ---------------------------------------------------------------------------

def forecast_revenue(
    *,
    free_agents: int,
    dev_agents: int,
    business_agents: int,
    enterprise_agents: int,
    avg_enterprise_monthly_usd: float,
    free_smbs: int,
    verified_smbs: int,
    featured_smbs: int,
    exclusive_smbs: int,
    avg_outcome_premium_per_op_usd: float,
    monthly_outcome_ops: int,
) -> dict:
    """Compute monthly revenue across all 5 streams."""
    # 1. Subscription revenue
    sub_rev = (
        dev_agents * _TIERS[SubscriptionTier.DEVELOPER].monthly_fee_usd
        + business_agents * _TIERS[SubscriptionTier.BUSINESS].monthly_fee_usd
        + enterprise_agents * avg_enterprise_monthly_usd
    )
    # 2. Listing revenue (SMB side)
    listing_rev = (
        verified_smbs * _LISTING_TIERS[ListingTier.VERIFIED].monthly_fee_usd
        + featured_smbs * _LISTING_TIERS[ListingTier.FEATURED].monthly_fee_usd
        + exclusive_smbs * _LISTING_TIERS[ListingTier.EXCLUSIVE].monthly_fee_usd
    )
    # 3. Outcome-based premium revenue
    outcome_rev = monthly_outcome_ops * avg_outcome_premium_per_op_usd
    # 4. Pay-as-you-go (overage from free tier)
    payg_rev = free_agents * 50 * 0.10  # assume avg 50 ops over free limit at $0.10
    total = sub_rev + listing_rev + outcome_rev + payg_rev
    return {
        "subscriptions_usd": round(sub_rev, 2),
        "listings_usd": round(listing_rev, 2),
        "outcome_premiums_usd": round(outcome_rev, 2),
        "payg_overage_usd": round(payg_rev, 2),
        "total_monthly_usd": round(total, 2),
        "total_annual_usd": round(total * 12, 2),
    }
