"""
Shared Pydantic models — single source of truth for all request/response schemas.
All /api/schemas/*.json files are exported from these models, not hand-authored.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

# Strict E.164: leading "+", first digit non-zero, total 8-16 digits.
_E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")


# Common fine-grained terms agents reach for, mapped to the three macro
# verticals we currently support. Capability is auto-set from the fine-grained
# term when the agent didn't supply one, so the directory search filters down
# instead of returning every business in the macro bucket.
_VERTICAL_ALIASES: dict[str, tuple[str, str | None]] = {
    # personal_services
    "haircut": ("personal_services", "haircut"),
    "salon": ("personal_services", None),
    "barber": ("personal_services", "haircut"),
    "barbershop": ("personal_services", "haircut"),
    "nail": ("personal_services", "manicure"),
    "nails": ("personal_services", "manicure"),
    "manicure": ("personal_services", "manicure"),
    "massage": ("personal_services", "swedish_massage"),
    "spa": ("personal_services", None),
    "yoga": ("personal_services", "yoga_class"),
    "fitness": ("personal_services", "personal_training"),
    "personal_training": ("personal_services", "personal_training"),
    "lash": ("personal_services", "lash_extensions"),
    "waxing": ("personal_services", "waxing"),
    "beauty": ("personal_services", None),
    # home_services
    "plumbing": ("home_services", "plumbing"),
    "plumber": ("home_services", "plumbing"),
    "electrician": ("home_services", "electrical_inspection"),
    "electrical": ("home_services", "electrical_inspection"),
    "hvac": ("home_services", None),
    "cleaning": ("home_services", "house_cleaning"),
    "house_cleaning": ("home_services", "house_cleaning"),
    "pest_control": ("home_services", "pest_inspection"),
    "pest": ("home_services", "pest_inspection"),
    "lawn": ("home_services", "lawn_mowing"),
    "landscaping": ("home_services", "landscaping"),
    "handyman": ("home_services", "general_handyman"),
    "roofing": ("home_services", "roof_inspection"),
    "roofer": ("home_services", "roof_inspection"),
    # professional_services
    "legal": ("professional_services", "legal_consultation"),
    "lawyer": ("professional_services", "legal_consultation"),
    "attorney": ("professional_services", "legal_consultation"),
    "law": ("professional_services", "legal_consultation"),
    "tax": ("professional_services", "tax_consultation"),
    "accounting": ("professional_services", "business_accounting"),
    "accountant": ("professional_services", "business_accounting"),
    "bookkeeping": ("professional_services", "bookkeeping"),
    "financial": ("professional_services", "financial_planning"),
    "financial_planning": ("professional_services", "financial_planning"),
    "tutor": ("professional_services", "math_tutoring"),
    "tutoring": ("professional_services", "math_tutoring"),
    "coaching": ("professional_services", "business_coaching"),
    "business_coaching": ("professional_services", "business_coaching"),
    "insurance": ("professional_services", "insurance_consultation"),
    "consulting": ("professional_services", None),
    # common health/restaurant terms — currently unsupported macro verticals;
    # we still alias them to the closest match rather than 500'ing, and the
    # find_business response will explain coverage honestly.
    "dentist": ("professional_services", "dental"),
    "dental": ("professional_services", "dental"),
    "doctor": ("professional_services", "medical_consultation"),
    "medical": ("professional_services", "medical_consultation"),
    "physician": ("professional_services", "medical_consultation"),
    "restaurant": ("personal_services", None),
    "dining": ("personal_services", None),
    "food": ("personal_services", None),
}


def alias_vertical(raw: str) -> tuple[str, str | None]:
    """Map a free-text vertical term to (macro_vertical, suggested_capability).

    Returns the macro vertical the agent should be searching under, plus a
    capability hint when the term implies a specific service. Falls back to
    the raw term if it's already a valid macro vertical.
    """
    key = (raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    if key in {"personal_services", "home_services", "professional_services"}:
        return (key, None)
    return _VERTICAL_ALIASES.get(key, (key, None))


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class OperationStatus(str, Enum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILURE = "failure"
    PENDING_ASYNC = "pending_async"


class ErrorCode(str, Enum):
    BAD_INPUT = "bad_input"
    MISSING_CAPABILITY = "missing_capability"
    RATE_LIMITED = "rate_limited"
    UPSTREAM_FAILURE = "upstream_failure"
    POLICY_BLOCKED = "policy_blocked"
    BUDGET_EXCEEDED = "budget_exceeded"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    TRANSIENT = "transient"
    INTERNAL = "internal"
    SUPPLY_UNREACHABLE = "supply_unreachable"
    SUPPLY_UNVERIFIED = "supply_unverified"
    OUT_OF_SUPPLY_NETWORK = "out_of_supply_network"
    COMPLIANCE_VIOLATION = "compliance_violation"
    OUT_OF_SCOPE = "out_of_scope"
    CONSENT_MISSING = "consent_missing"
    RECORDING_CONSENT_MISSING = "recording_consent_missing"


class ErrorCategory(str, Enum):
    CLIENT_ERROR = "client_error"
    SERVER_ERROR = "server_error"
    COMPLIANCE_ERROR = "compliance_error"
    POLICY_ERROR = "policy_error"


class Vertical(str, Enum):
    PERSONAL_SERVICES = "personal_services"
    HOME_SERVICES = "home_services"
    PROFESSIONAL_SERVICES = "professional_services"


class MessageType(str, Enum):
    # Public API surface intentionally excludes "marketing". This service only
    # facilitates consumer-initiated transactional flows; unsolicited outbound
    # campaigns are out of scope and rejected at schema-validation time.
    # compliance/pre_check.py still inspects the string "marketing" as a
    # belt-and-braces guard against any internal caller mis-tagging a message.
    TRANSACTIONAL = "transactional"
    REMINDER = "reminder"
    FOLLOW_UP = "follow_up"
    NOTIFICATION = "notification"


class ChannelPreference(str, Enum):
    SMS = "sms"
    EMAIL = "email"
    VOICE = "voice"
    AUTO = "auto"


class AppointmentAction(str, Enum):
    BOOK = "book"
    RESCHEDULE = "reschedule"
    CANCEL = "cancel"
    CHECK_AVAILABILITY = "check_availability"


class ConfirmationType(str, Enum):
    OTP = "otp"
    BOOKING_CONFIRMATION = "booking_confirmation"
    PAYMENT_RECEIPT = "payment_receipt"
    CANCELLATION_NOTICE = "cancellation_notice"
    REMINDER = "reminder"


class InboundChannel(str, Enum):
    SMS = "sms"
    EMAIL = "email"
    VOICE_VOICEMAIL = "voice_voicemail"
    WEB_FORM = "web_form"
    API = "api"


class EscalationReason(str, Enum):
    AUTOMATION_FAILED = "automation_failed"
    CUSTOMER_REQUESTED = "customer_requested"
    COMPLIANCE_HOLD = "compliance_hold"
    AMBIGUOUS_INTENT = "ambiguous_intent"
    EXCEPTION_REQUIRED = "exception_required"


class ExecutionProfile(str, Enum):
    SYNC = "sync"
    SYNC_FAST = "sync_fast"
    ASYNC_BY_DEFAULT = "async_by_default"


class FailureClass(str, Enum):
    DISCOVERY_MISS = "discovery_miss"
    SELECTION_MISS = "selection_miss"
    PARAM_MISUSE = "param_misuse"
    EXECUTION_FAILURE = "execution_failure"
    OUTCOME_REJECTED = "outcome_rejected"
    SUPPLY_UNREACHABLE = "supply_unreachable"
    COMPLIANCE_VIOLATION = "compliance_violation"
    ENVIRONMENTAL = "environmental"


# ---------------------------------------------------------------------------
# Cost model
# ---------------------------------------------------------------------------

class CostRecord(BaseModel):
    amount: float
    currency: str = "USD"
    basis: str


# ---------------------------------------------------------------------------
# OutcomeReceipt — every operation returns one of these
# ---------------------------------------------------------------------------

class OutcomeReceipt(BaseModel):
    operation_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    status: OperationStatus
    reason_code: Optional[str] = None
    human_message: Optional[str] = None
    result: Optional[dict[str, Any]] = None
    cost: Optional[CostRecord] = None
    latency_ms: Optional[int] = None
    channel_used: Optional[str] = None
    channel_fallback_chain: list[str] = Field(default_factory=list)
    estimated_completion_time: Optional[datetime] = None
    next_actions: list[str] = Field(default_factory=list)
    retriable: bool = False
    trace_id: Optional[str] = None


# ---------------------------------------------------------------------------
# APIError — structured error response
# ---------------------------------------------------------------------------

class APIError(BaseModel):
    code: ErrorCode
    category: ErrorCategory
    retriable: bool
    message: str
    next_action: Optional[str] = None
    retry_after_ms: Optional[int] = None
    violation_detail: Optional[dict[str, Any]] = None
    required_scope: Optional[dict[str, Any]] = None
    trace_id: Optional[str] = None


class ComplianceViolationError(Exception):
    def __init__(self, rule: str, recipient_id: str, channel: str, jurisdiction: str, message: str):
        self.rule = rule
        self.recipient_id = recipient_id
        self.channel = channel
        self.jurisdiction = jurisdiction
        self.message = message
        super().__init__(message)

    def to_api_error(self) -> APIError:
        return APIError(
            code=ErrorCode.COMPLIANCE_VIOLATION,
            category=ErrorCategory.COMPLIANCE_ERROR,
            retriable=False,
            message=self.message,
            next_action="Obtain required consent for this recipient+channel+use_case before retrying.",
            violation_detail={
                "rule": self.rule,
                "recipient_id": self.recipient_id,
                "channel": self.channel,
                "jurisdiction": self.jurisdiction,
            },
        )


# ---------------------------------------------------------------------------
# Agent-Identity
# ---------------------------------------------------------------------------

class PrincipalKind(str, Enum):
    CONSUMER = "consumer"
    BUSINESS = "business"


class Principal(BaseModel):
    kind: PrincipalKind
    id: str


class AgentScope(BaseModel):
    operations: list[str]  # ["*"] for all
    budget_cap: float
    verticals: Optional[list[Vertical]] = None


class AgentIdentity(BaseModel):
    agent_id: str
    principal: Optional[Principal] = None
    scope: AgentScope
    expiry: datetime
    issuer: str


# ---------------------------------------------------------------------------
# find_business request / response
# ---------------------------------------------------------------------------

class LocationFilter(BaseModel):
    zip_or_city: str
    radius_miles: float = 10.0


class PriceBand(BaseModel):
    max_usd: Optional[float] = None


class AvailabilityWindow(BaseModel):
    start_iso: Optional[datetime] = None
    end_iso: Optional[datetime] = None


class FindBusinessRequest(BaseModel):
    vertical: Vertical
    location: LocationFilter
    capability: Optional[str] = None
    price_band: Optional[PriceBand] = None
    availability_window: Optional[AvailabilityWindow] = None
    max_results: int = Field(default=5, le=20)

    @model_validator(mode="before")
    @classmethod
    def _alias_natural_terms(cls, data: Any) -> Any:
        # Agents frequently send fine-grained verticals ("plumbing", "dentist")
        # rather than our three macro buckets. Translate them in-place so the
        # request validates and the directory search still narrows by capability.
        if not isinstance(data, dict):
            return data
        raw = data.get("vertical")
        if isinstance(raw, str):
            macro, cap_hint = alias_vertical(raw)
            data["vertical"] = macro
            if cap_hint and not data.get("capability"):
                data["capability"] = cap_hint
        return data


class SMBRecord(BaseModel):
    smb_id: str
    name: str
    vertical: Vertical
    address: Optional[str] = None
    capabilities: list[str] = Field(default_factory=list)
    channels_available: list[str] = Field(default_factory=list)
    price_range: Optional[dict[str, Any]] = None
    verified_at: Optional[datetime] = None
    rank_score: Optional[float] = None
    is_demo: bool = False


# ---------------------------------------------------------------------------
# verify_business
# ---------------------------------------------------------------------------

class VerifyBusinessRequest(BaseModel):
    smb_id: str
    capability_to_verify: Optional[str] = None


# ---------------------------------------------------------------------------
# send_message
# ---------------------------------------------------------------------------

class RecipientIdType(str, Enum):
    PHONE = "phone"
    EMAIL = "email"
    SMB_ID = "smb_id"
    CUSTOMER_ID = "customer_id"


class Recipient(BaseModel):
    id_type: RecipientIdType
    id_value: str
    country_code: Optional[str] = None

    @model_validator(mode="after")
    def _validate_phone_e164(self) -> "Recipient":
        # Reject malformed phone numbers at schema validation time so callers
        # get a clear "id_value must be E.164" error instead of a confusing
        # compliance-layer rejection downstream. Only phones are validated
        # here — emails / smb_id / customer_id have their own validators or
        # are opaque identifiers.
        if self.id_type == RecipientIdType.PHONE:
            if not _E164_RE.match(self.id_value or ""):
                raise ValueError(
                    "id_value must be E.164 (e.g., +14045550100)"
                )
        return self


class MessageContent(BaseModel):
    body: str
    subject: Optional[str] = None
    template_id: Optional[str] = None
    template_vars: Optional[dict[str, Any]] = None


class SendMessageRequest(BaseModel):
    recipient: Recipient
    message_type: MessageType
    content: MessageContent
    preferred_channel: ChannelPreference = ChannelPreference.AUTO
    send_at_iso: Optional[datetime] = None


# ---------------------------------------------------------------------------
# capture_lead
# ---------------------------------------------------------------------------

class ProspectData(BaseModel):
    name: str
    phone: Optional[str] = None
    email: Optional[str] = None
    service_interest: Optional[str] = None
    notes: Optional[str] = None
    consent_record_id: Optional[str] = None


class CaptureLeadRequest(BaseModel):
    smb_id: str
    prospect: ProspectData
    source: Optional[str] = None


# ---------------------------------------------------------------------------
# schedule_appointment
# ---------------------------------------------------------------------------

class CustomerInfo(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None


class RequestedTime(BaseModel):
    preferred_iso: Optional[datetime] = None
    window_start_iso: Optional[datetime] = None
    window_end_iso: Optional[datetime] = None
    duration_minutes: Optional[int] = None


class ScheduleAppointmentRequest(BaseModel):
    smb_id: str
    action: AppointmentAction
    service: Optional[str] = None
    customer: Optional[CustomerInfo] = None
    requested_time: Optional[RequestedTime] = None
    existing_appointment_id: Optional[str] = None
    notes: Optional[str] = None


# ---------------------------------------------------------------------------
# send_transactional_confirmation
# ---------------------------------------------------------------------------

class TransactionalRecipient(BaseModel):
    phone_or_email: str
    name: Optional[str] = None


class SendTransactionalConfirmationRequest(BaseModel):
    recipient: TransactionalRecipient
    confirmation_type: ConfirmationType
    data: dict[str, Any]
    preferred_channel: ChannelPreference = ChannelPreference.SMS


# ---------------------------------------------------------------------------
# handle_inbound
# ---------------------------------------------------------------------------

class InboundSender(BaseModel):
    phone: Optional[str] = None
    email: Optional[str] = None
    name: Optional[str] = None


class HandleInboundRequest(BaseModel):
    smb_id: str
    inbound_channel: InboundChannel
    sender: Optional[InboundSender] = None
    raw_message: str
    received_at_iso: Optional[datetime] = None
    routing_rules: Optional[dict[str, Any]] = None


# ---------------------------------------------------------------------------
# escalate_to_human
# ---------------------------------------------------------------------------

class EscalationContext(BaseModel):
    original_operation: Optional[str] = None
    operation_id: Optional[str] = None
    transcript: Optional[list[dict[str, Any]]] = None
    prior_actions: Optional[list[dict[str, Any]]] = None
    recommended_next_step: Optional[str] = None


class EscalateToHumanRequest(BaseModel):
    smb_id: str
    reason: EscalationReason
    context: EscalationContext
    priority: str = "normal"


# ---------------------------------------------------------------------------
# get_status / get_outcome
# ---------------------------------------------------------------------------

class GetStatusResponse(BaseModel):
    operation_id: str
    status: str
    estimated_completion_time: Optional[datetime] = None
    last_updated_at: Optional[datetime] = None
    partial_result: Optional[dict[str, Any]] = None


# ---------------------------------------------------------------------------
# preview_cost
# ---------------------------------------------------------------------------

class PreviewCostRequest(BaseModel):
    operation: str
    params: dict[str, Any]


class PreviewCostResponse(BaseModel):
    estimated_cost_usd: float
    cost_range: dict[str, float]
    estimated_latency_p50_ms: int
    estimated_latency_p95_ms: int
    success_probability_estimate: float
    channel_likely: str
    cost_accuracy_slo: str = "±5%"


# ---------------------------------------------------------------------------
# webhook registration
# ---------------------------------------------------------------------------

class WebhookRegistrationRequest(BaseModel):
    url: str
    secret: str
    operations: Optional[list[str]] = None


class WebhookRegistrationResponse(BaseModel):
    webhook_id: str
    verified: bool
