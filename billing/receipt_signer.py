"""
Receipt signer — signs cost receipts so agents can verify billing integrity.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from typing import Any

_DEFAULT_SIGNING_KEY = "dev-signing-key-replace-in-production"
_SIGNING_KEY = os.getenv("BILLING_SIGNING_KEY", _DEFAULT_SIGNING_KEY)

# LOW-1 fix: assert that the default signing key is NOT in use when running in
# production. Anyone with access to this source can read the default and forge
# receipts, undermining billing integrity. BILLING_SIGNING_KEY must be set as a
# Render secret (sync: false in render.yaml) before enabling x402 in production.
#
# Note: this assertion runs at import time so the process fails fast at startup
# rather than silently accepting forged receipts. render.yaml already declares
# BILLING_SIGNING_KEY as sync:false (added in the x402-payment-safety fix).
_ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
if _ENVIRONMENT == "production" and _SIGNING_KEY == _DEFAULT_SIGNING_KEY:
    raise RuntimeError(
        "BILLING_SIGNING_KEY is set to the default dev value in production. "
        "Set a random secret via the Render dashboard (Environment > BILLING_SIGNING_KEY). "
        "Do NOT commit the key — use sync: false in render.yaml."
    )


def sign_receipt(receipt: dict[str, Any]) -> str:
    """Return HMAC-SHA256 signature of the receipt JSON."""
    body = json.dumps(receipt, sort_keys=True).encode()
    return hmac.new(_SIGNING_KEY.encode(), body, hashlib.sha256).hexdigest()


def verify_receipt(receipt: dict[str, Any], signature: str) -> bool:
    expected = sign_receipt(receipt)
    return hmac.compare_digest(expected, signature)
