"""
Receipt signer — signs cost receipts so agents can verify billing integrity.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from typing import Any

_SIGNING_KEY = os.getenv("BILLING_SIGNING_KEY", "dev-signing-key-replace-in-production")


def sign_receipt(receipt: dict[str, Any]) -> str:
    """Return HMAC-SHA256 signature of the receipt JSON."""
    body = json.dumps(receipt, sort_keys=True).encode()
    return hmac.new(_SIGNING_KEY.encode(), body, hashlib.sha256).hexdigest()


def verify_receipt(receipt: dict[str, Any], signature: str) -> bool:
    expected = sign_receipt(receipt)
    return hmac.compare_digest(expected, signature)
