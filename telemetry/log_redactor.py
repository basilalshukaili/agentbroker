"""
Log redactor — strips PII from log lines before emission.
Phone numbers, emails, and names are replaced with hashed tokens.
"""
from __future__ import annotations

import hashlib
import re

_PHONE_RE = re.compile(r'\+?[\d\s\-().]{10,15}')
_EMAIL_RE = re.compile(r'[\w.+-]+@[\w-]+\.[\w.]+')
_REDACT_TOKEN = "[REDACTED]"


def _hash_token(value: str) -> str:
    return "[hash:" + hashlib.sha256(value.encode()).hexdigest()[:8] + "]"


def redact(text: str, hash_mode: bool = True) -> str:
    """Replace PII patterns in a string with hash tokens or REDACTED."""
    replace = _hash_token if hash_mode else lambda _: _REDACT_TOKEN
    text = _EMAIL_RE.sub(lambda m: replace(m.group()), text)
    text = _PHONE_RE.sub(lambda m: replace(m.group()) if len(m.group().replace(" ","").replace("-","")) >= 10 else m.group(), text)
    return text


def redact_dict(data: dict, sensitive_keys: set[str] | None = None) -> dict:
    """Recursively redact sensitive keys from a dict."""
    _SENSITIVE = sensitive_keys or {"phone", "email", "name", "recipient_id", "id_value", "prospect"}
    result = {}
    for k, v in data.items():
        if k in _SENSITIVE:
            if isinstance(v, str):
                result[k] = _hash_token(v)
            else:
                result[k] = _REDACT_TOKEN
        elif isinstance(v, dict):
            result[k] = redact_dict(v, sensitive_keys)
        elif isinstance(v, list):
            result[k] = [redact_dict(i, sensitive_keys) if isinstance(i, dict) else i for i in v]
        else:
            result[k] = v
    return result
