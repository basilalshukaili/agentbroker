"""
10DLC campaign registry scaffold.
Tracks brand registration, campaign registration, and per-campaign SMS traffic authorization.
10DLC registration is a precondition for any A2P SMS traffic in the US.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class CampaignStatus(str, Enum):
    PENDING = "pending"
    REGISTERED = "registered"
    SUSPENDED = "suspended"
    REJECTED = "rejected"


class UseCaseType(str, Enum):
    CUSTOMER_CARE = "CUSTOMER_CARE"
    DELIVERY_NOTIFICATION = "DELIVERY_NOTIFICATION"
    MARKETING = "MARKETING"
    MIXED = "MIXED"
    POLLING_VOTING = "POLLING_VOTING"
    PUBLIC_SERVICE_ANNOUNCEMENT = "PUBLIC_SERVICE_ANNOUNCEMENT"
    SECURITY_ALERT = "SECURITY_ALERT"
    ACCOUNT_NOTIFICATION = "ACCOUNT_NOTIFICATION"
    TWO_FACTOR_AUTHENTICATION = "TWO_FACTOR_AUTHENTICATION"
    APPOINTMENT_REMINDER = "APPOINTMENT_REMINDER"


@dataclass
class BrandRegistration:
    brand_id: str
    company_name: str
    ein: str               # Employer Identification Number
    street: str
    city: str
    state: str
    zip_code: str
    country: str = "US"
    vertical: str = "SMB_SERVICES"
    status: CampaignStatus = CampaignStatus.PENDING
    registered_at: Optional[datetime] = None
    tcr_brand_id: Optional[str] = None  # The Campaign Registry ID


@dataclass
class CampaignRegistration:
    campaign_id: str
    brand_id: str
    use_case: UseCaseType
    description: str
    sample_messages: list[str] = field(default_factory=list)
    opt_in_workflow: str = ""
    opt_out_keywords: list[str] = field(default_factory=lambda: ["STOP", "UNSUBSCRIBE", "CANCEL", "END"])
    help_keywords: list[str] = field(default_factory=lambda: ["HELP", "INFO"])
    status: CampaignStatus = CampaignStatus.PENDING
    registered_at: Optional[datetime] = None
    tcr_campaign_id: Optional[str] = None


class CampaignRegistry:
    """
    In-memory scaffold; production integrates with The Campaign Registry (TCR) API
    and the Twilio Messaging Service API for campaign assignment.
    """

    def __init__(self) -> None:
        self._brands: dict[str, BrandRegistration] = {}
        self._campaigns: dict[str, CampaignRegistration] = {}

    def register_brand(self, brand: BrandRegistration) -> BrandRegistration:
        """Initiate brand registration with TCR. Stub returns pending."""
        self._brands[brand.brand_id] = brand
        return brand

    def register_campaign(self, campaign: CampaignRegistration) -> CampaignRegistration:
        """Initiate campaign registration with TCR. Stub returns pending."""
        if campaign.brand_id not in self._brands:
            raise ValueError(f"Brand {campaign.brand_id} not registered")
        self._campaigns[campaign.campaign_id] = campaign
        return campaign

    def is_sms_authorized(self, use_case: UseCaseType) -> bool:
        """
        Returns True if there is a registered (non-pending, non-rejected) campaign
        for the given use case. This is the gate called by the SMS compliance pre-check.
        """
        for campaign in self._campaigns.values():
            if campaign.use_case == use_case and campaign.status == CampaignStatus.REGISTERED:
                return True
        return False

    def get_campaign_for_use_case(self, use_case: UseCaseType) -> CampaignRegistration | None:
        for campaign in self._campaigns.values():
            if campaign.use_case == use_case and campaign.status == CampaignStatus.REGISTERED:
                return campaign
        return None


_registry = CampaignRegistry()


def get_campaign_registry() -> CampaignRegistry:
    return _registry
