"""
Centralized configuration — reads from environment variables with safe defaults.
All secrets MUST be set via environment variables in production. Never commit secrets.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, str(default)))
    except ValueError:
        return default


def _env_bool(key: str, default: bool = False) -> bool:
    val = os.environ.get(key, str(default)).lower()
    return val in ("1", "true", "yes")


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

SERVICE_NAME = _env("SERVICE_NAME", "smb-broker")
SERVICE_VERSION = _env("SERVICE_VERSION", "0.1.0")
ENVIRONMENT = _env("ENVIRONMENT", "development")   # development | staging | production
DEBUG = _env_bool("DEBUG", default=ENVIRONMENT == "development")
LOG_LEVEL = _env("LOG_LEVEL", "INFO")

# ---------------------------------------------------------------------------
# API / Auth
# ---------------------------------------------------------------------------

AGENT_IDENTITY_SIGNING_SECRET = _env(
    "AGENT_IDENTITY_SIGNING_SECRET",
    "dev-secret-CHANGE-IN-PRODUCTION-minimum-32-chars",
)
TOKEN_TTL_SECONDS = _env_int("TOKEN_TTL_SECONDS", 86400)    # 24h
REQUIRE_AUTH = _env_bool("REQUIRE_AUTH", default=ENVIRONMENT == "production")

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

DATABASE_URL = _env(
    "DATABASE_URL",
    "postgresql://smb_broker:smb_broker@localhost:5432/smb_broker",
)

# ---------------------------------------------------------------------------
# Redis / Celery
# ---------------------------------------------------------------------------

REDIS_URL = _env("REDIS_URL", "redis://localhost:6379/0")
CELERY_BROKER_URL = _env("CELERY_BROKER_URL", REDIS_URL)
CELERY_RESULT_BACKEND = _env("CELERY_RESULT_BACKEND", REDIS_URL)

# ---------------------------------------------------------------------------
# Channel credentials
# ---------------------------------------------------------------------------

TWILIO_ACCOUNT_SID = _env("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = _env("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM_NUMBER = _env("TWILIO_FROM_NUMBER", "")

SENDGRID_API_KEY = _env("SENDGRID_API_KEY", "")
SENDGRID_FROM_EMAIL = _env("SENDGRID_FROM_EMAIL", "noreply@agent-broker-edge.basil-agent.workers.dev")

VAPI_API_KEY = _env("VAPI_API_KEY", "")
BLAND_API_KEY = _env("BLAND_API_KEY", "")

CALCOM_API_KEY = _env("CALCOM_API_KEY", "")

# ---------------------------------------------------------------------------
# Compliance
# ---------------------------------------------------------------------------

COMPLIANCE_STRICT_MODE = _env_bool("COMPLIANCE_STRICT_MODE", default=True)
AUDIT_LOG_RETENTION_DAYS = _env_int("AUDIT_LOG_RETENTION_DAYS", 365)

# ---------------------------------------------------------------------------
# Billing
# ---------------------------------------------------------------------------

BILLING_RECEIPT_SIGNING_SECRET = _env(
    "BILLING_RECEIPT_SIGNING_SECRET",
    "billing-secret-CHANGE-IN-PRODUCTION",
)
DEFAULT_BUDGET_CAP_USD = _env_float("DEFAULT_BUDGET_CAP_USD", 10.0)

# ---------------------------------------------------------------------------
# x402 — agent-native USDC micropayments (Coinbase CDP facilitator + Bazaar)
# ---------------------------------------------------------------------------
# The standard x402 scheme: an agent sends a signed EIP-3009 authorization
# (in the MCP `_meta["x402/payment"]` field or the HTTP `X-PAYMENT` header);
# we VERIFY + SETTLE it through the Coinbase CDP facilitator. The first settled
# payment auto-lists us in the x402 Bazaar (semantic discovery for agents).
#
# Disabled by default — set X402_ENABLED=true in prod once CDP keys + receiver
# are configured. When disabled, paid tools run free (current behavior) so the
# server never breaks if x402 is misconfigured.
X402_ENABLED = _env_bool("X402_ENABLED", default=False)
# Where settled USDC lands. Basil's Binance USDC-on-Base deposit address.
X402_RECEIVER_ADDRESS = _env("X402_RECEIVER_ADDRESS", "")
# Coinbase CDP facilitator (verify + settle). Appends /verify, /settle, /supported.
X402_FACILITATOR_URL = _env(
    "X402_FACILITATOR_URL", "https://api.cdp.coinbase.com/platform/v2/x402"
)
# Base mainnet (CAIP-2). The production settlement network.
X402_NETWORK = _env("X402_NETWORK", "eip155:8453")
# Also accept Base Sepolia testnet (eip155:84532) payments — for $0 end-to-end
# validation only. NEVER enable in prod: testnet USDC is worthless, so accepting
# it would give away real service for free.
X402_ENABLE_TESTNET = _env_bool("X402_ENABLE_TESTNET", default=False)
# CDP API key (EdDSA / Ed25519). Secret is 64-byte base64 (first 32 = seed).
CDP_API_KEY_ID = _env("CDP_API_KEY_ID", "")
CDP_API_KEY_SECRET = _env("CDP_API_KEY_SECRET", "")
# Public MCP endpoint advertised in the Bazaar discovery `resource` so agents
# who discover a paid tool know where to connect. Defaults to the edge worker.
PUBLIC_BASE_URL = _env("PUBLIC_BASE_URL", "https://agent-broker-edge.basil-agent.workers.dev")
X402_PUBLIC_MCP_URL = _env("X402_PUBLIC_MCP_URL", PUBLIC_BASE_URL.rstrip("/") + "/mcp")

# ---------------------------------------------------------------------------
# Webhook delivery
# ---------------------------------------------------------------------------

WEBHOOK_MAX_RETRIES = _env_int("WEBHOOK_MAX_RETRIES", 10)
WEBHOOK_INITIAL_BACKOFF_SECONDS = _env_int("WEBHOOK_INITIAL_BACKOFF_SECONDS", 5)

# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------

TELEMETRY_ENABLED = _env_bool("TELEMETRY_ENABLED", default=True)
OTEL_EXPORTER_ENDPOINT = _env("OTEL_EXPORTER_ENDPOINT", "")

# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

REPORTS_DIR = _env("REPORTS_DIR", "reports")

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_production_config() -> list[str]:
    """
    Returns a list of warnings for missing production secrets.
    Call this at startup when ENVIRONMENT=production.
    """
    warnings: list[str] = []
    required_in_prod = [
        ("AGENT_IDENTITY_SIGNING_SECRET", AGENT_IDENTITY_SIGNING_SECRET),
        ("DATABASE_URL", DATABASE_URL),
        ("REDIS_URL", REDIS_URL),
        ("TWILIO_ACCOUNT_SID", TWILIO_ACCOUNT_SID),
        ("TWILIO_AUTH_TOKEN", TWILIO_AUTH_TOKEN),
        ("SENDGRID_API_KEY", SENDGRID_API_KEY),
        ("BILLING_RECEIPT_SIGNING_SECRET", BILLING_RECEIPT_SIGNING_SECRET),
    ]
    for name, val in required_in_prod:
        if not val or "CHANGE-IN-PRODUCTION" in val or val == "":
            warnings.append(f"MISSING or DEFAULT value for required production secret: {name}")
    return warnings
