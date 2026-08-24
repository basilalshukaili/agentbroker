"""
Centralized configuration — reads from environment variables with safe defaults.
All secrets MUST be set via environment variables in production. Never commit secrets.

Render SECRET FILES: Render mounts secret files at /etc/secrets/<name> (and at
the project root as a fallback). They are NOT injected as environment variables
automatically, so os.getenv() returns None without the hydration step below.
hydrate_env_from_secret_files() runs before any config variable is resolved and
populates os.environ from those files, making the rest of this module work
identically whether a value was supplied as a real env var or a secret file.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path

_log = logging.getLogger("smb_broker.config")

# Regex for a valid environment-variable name: starts with uppercase letter,
# followed by zero or more uppercase letters, digits, or underscores.
_ENV_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

# Directories to search, in priority order.
# /etc/secrets is Render's canonical mount point for secret files.
# The project root (CWD) is a documented fallback Render also uses.
_SECRET_DIRS = [
    Path("/etc/secrets"),
    Path("."),
]


def hydrate_env_from_secret_files(
    extra_dirs: list[Path] | None = None,
) -> list[str]:
    """
    Load Render secret files into os.environ before config variables are read.

    For each candidate directory (primarily /etc/secrets, then CWD), iterates
    its top-level entries. Any file whose name matches ^[A-Z][A-Z0-9_]*$ is
    treated as an environment variable: if the variable is not already set in
    os.environ, its value is read from the file and injected.

    Rules:
    - Never overwrites an already-set env var (env var wins over secret file).
    - Silently skips directories that do not exist (safe on local dev).
    - Skips sub-directories and files with non-ENV-key names.
    - Never logs values, only key names.
    - All errors are caught and logged as warnings -- startup never crashes.

    Returns the list of key names that were hydrated (names only).
    """
    dirs = list(_SECRET_DIRS)
    if extra_dirs:
        dirs = list(extra_dirs) + dirs

    hydrated: list[str] = []
    seen_dirs: set[str] = set()

    for d in dirs:
        try:
            resolved = str(d.resolve())
            if resolved in seen_dirs:
                continue
            seen_dirs.add(resolved)

            if not d.exists() or not d.is_dir():
                continue

            for entry in d.iterdir():
                try:
                    if not entry.is_file():
                        continue
                    name = entry.name
                    if not _ENV_KEY_RE.match(name):
                        continue
                    if os.environ.get(name):
                        # Already set -- do not overwrite.
                        continue
                    value = entry.read_text(encoding="utf-8").strip()
                    if not value:
                        continue
                    os.environ[name] = value
                    hydrated.append(name)
                    _log.info("secret_file_hydrated key=%s source=%s", name, d)
                except Exception as exc:  # noqa: BLE001
                    _log.warning(
                        "secret_file_hydration_skipped entry=%s err=%s", entry, exc
                    )
        except Exception as exc:  # noqa: BLE001
            _log.warning("secret_file_hydration_dir_error dir=%s err=%s", d, exc)

    if hydrated:
        _log.info(
            "secret_file_hydration_complete count=%d keys=%s",
            len(hydrated),
            sorted(hydrated),
        )
    return hydrated


# Hydrate from secret files BEFORE any os.environ.get() call below so that
# downstream config variables pick up values whether they were supplied as
# real environment variables or as Render secret files.
hydrate_env_from_secret_files()


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
# Supabase -- durable storage for billing_events and smb_supply tables
# ---------------------------------------------------------------------------
SUPABASE_URL = _env("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = _env("SUPABASE_SERVICE_KEY", "")  # server-side only

# ---------------------------------------------------------------------------
# Redis / Celery
# ---------------------------------------------------------------------------

REDIS_URL = _env("REDIS_URL", "redis://localhost:6379/0")
CELERY_BROKER_URL = _env("CELERY_BROKER_URL", REDIS_URL)
CELERY_RESULT_BACKEND = _env("CELERY_RESULT_BACKEND", REDIS_URL)

# ---------------------------------------------------------------------------
# Channel credentials
# ---------------------------------------------------------------------------

# Twilio — two auth modes supported:
#   API-Key mode (preferred, use for API Keys created under a project/subaccount):
#     TWILIO_API_KEY_SID    — starts with "SK"
#     TWILIO_API_KEY_SECRET — the secret for the above API Key
#     TWILIO_ACCOUNT_SID   — the Account SID (starts with "AC") for the API Key
#   Legacy mode (Account SID + Auth Token, still supported):
#     TWILIO_ACCOUNT_SID   — your Account SID (starts with "AC")
#     TWILIO_AUTH_TOKEN    — the Auth Token from the Twilio console
#
# Sender — exactly one must be set:
#   TWILIO_MESSAGING_SERVICE_SID — preferred; 10DLC-registered Messaging Service
#   TWILIO_FROM_NUMBER            — E.164 fallback (e.g. "+15005550006")
TWILIO_ACCOUNT_SID = _env("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = _env("TWILIO_AUTH_TOKEN", "")           # legacy mode only
TWILIO_API_KEY_SID = _env("TWILIO_API_KEY_SID", "")         # API-Key mode (SK...)
TWILIO_API_KEY_SECRET = _env("TWILIO_API_KEY_SECRET", "")   # API-Key mode secret
TWILIO_MESSAGING_SERVICE_SID = _env("TWILIO_MESSAGING_SERVICE_SID", "")  # preferred sender
TWILIO_FROM_NUMBER = _env("TWILIO_FROM_NUMBER", "")          # fallback sender (E.164)

SENDGRID_API_KEY = _env("SENDGRID_API_KEY", "")
SENDGRID_FROM_EMAIL = _env("SENDGRID_FROM_EMAIL", "noreply@hatchloop.dev")

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
# Credits billing (slice 3+4)
# ---------------------------------------------------------------------------
# CREDITS_ENABLED=false (default) means the server behaves exactly as today.
# Flip to true ONLY after: (a) Polar packages created with metadata.credits,
# (b) POLAR_PACKAGES env set, (c) end-to-end credit path verified.
CREDITS_ENABLED = _env_bool("CREDITS_ENABLED", default=False)
# Courtesy grant for existing paid-key holders when CREDITS_ENABLED first flips on.
GRANDFATHER_CREDITS = _env_int("GRANDFATHER_CREDITS", 1000)
# Free-signup grant: credits issued to a new account with no Polar purchase.
FREE_SIGNUP_CREDITS = _env_int("FREE_SIGNUP_CREDITS", 100)

# ---------------------------------------------------------------------------
# Data tool freemium metering (DATA_METERING_ENABLED)
# ---------------------------------------------------------------------------
# When false (default): verify_company_record, screen_sanctions,
# map_trade_restriction run free/unmetered -- exactly as before this feature.
# When true: those tools are gated by a per-caller daily free quota.
#   Within quota: call runs free (quota decremented).
#   Beyond quota: x402 (if payment present) -> credits (if funded account) ->
#                 honest failure with reason_code=free_quota_exceeded (cost=0).
# Go-live requires founder approval of quotas + $0.02 price, then flip to true.
DATA_METERING_ENABLED = _env_bool("DATA_METERING_ENABLED", default=False)
# Daily free quota for email-verified free-key holders (in-memory, per process restart).
FREE_DATA_QUOTA_PER_DAY = _env_int("FREE_DATA_QUOTA_PER_DAY", 50)
# Daily free quota for anonymous callers (tracked by sha256(ip)+date in Supabase,
# best-effort: fail-open when Supabase is unavailable or IP is unknown).
ANON_DATA_QUOTA_PER_DAY = _env_int("ANON_DATA_QUOTA_PER_DAY", 20)

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
PUBLIC_BASE_URL = _env("PUBLIC_BASE_URL", "https://api.hatchloop.dev")
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
        ("SENDGRID_API_KEY", SENDGRID_API_KEY),
        ("BILLING_RECEIPT_SIGNING_SECRET", BILLING_RECEIPT_SIGNING_SECRET),
    ]
    for name, val in required_in_prod:
        if not val or "CHANGE-IN-PRODUCTION" in val or val == "":
            warnings.append(f"MISSING or DEFAULT value for required production secret: {name}")

    # Twilio auth: at least one mode must be configured (API-Key preferred over legacy)
    api_key_mode_ok = bool(TWILIO_API_KEY_SID and TWILIO_API_KEY_SECRET and TWILIO_ACCOUNT_SID)
    legacy_mode_ok = bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN)
    if not api_key_mode_ok and not legacy_mode_ok:
        warnings.append(
            "MISSING Twilio auth: set TWILIO_ACCOUNT_SID + TWILIO_API_KEY_SID + "
            "TWILIO_API_KEY_SECRET (API-Key mode, preferred) OR "
            "TWILIO_ACCOUNT_SID + TWILIO_AUTH_TOKEN (legacy mode)"
        )

    # Twilio sender: at least one must be set
    if not TWILIO_MESSAGING_SERVICE_SID and not TWILIO_FROM_NUMBER:
        warnings.append(
            "MISSING Twilio sender: set TWILIO_MESSAGING_SERVICE_SID "
            "(preferred, 10DLC-registered) or TWILIO_FROM_NUMBER (E.164 fallback)"
        )

    return warnings
