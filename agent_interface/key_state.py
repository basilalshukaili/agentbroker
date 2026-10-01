"""What did the caller present as a key, and was it any good?

WHY THIS FILE EXISTS. `agent_id_from_token` answers a different question - "who is this?" - and
returns the sentinel "anonymous" for an empty header, a wrong key, an expired key, a revoked key
and a placeholder that was never filled in, all alike. That is correct for authorisation and
disastrous for support: a real key holder whose key is failing is indistinguishable, in every
table and every response, from a stranger who sent no key at all. The 2026-09-30 audit found one
live example - an external client sending a 33-character `env:`-style placeholder - that got free
tools, an `auth_required` on the first write tool, and no hint that its own key was the problem.

So this module keeps the distinction: absent / valid / invalid / expired / placeholder, with a
machine reason and a sentence a human (or a model) can act on.

It never raises, never logs and never echoes the presented value: the header may be a real secret
pasted into the wrong place, and the one thing a diagnostic must not do is copy it somewhere.
Validation itself is `identity.validate_token` - this module classifies its verdict, it does not
re-implement it, so there is exactly one definition of a valid key.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Optional

KEY_NONE = "none"
KEY_VALID = "valid"
KEY_INVALID = "invalid"
KEY_EXPIRED = "expired"
KEY_PLACEHOLDER = "placeholder"

KEY_STATES = (KEY_NONE, KEY_VALID, KEY_INVALID, KEY_EXPIRED, KEY_PLACEHOLDER)

# A header whose presence means "the caller tried to authenticate and it did not work".
PROBLEM_STATES = frozenset({KEY_INVALID, KEY_EXPIRED, KEY_PLACEHOLDER})

# A real key is `<base64url payload>.<64 hex>`. Anything that already looks like that is judged by
# its signature, never by its words.
_KEY_SHAPE = re.compile(r"^[A-Za-z0-9_\-]{8,}\.[0-9a-f]{64}$")

# Prefixes that mean "a config template was never expanded": `env:NAME`, `${NAME}`, `$NAME`,
# `{{NAME}}`, `<your-key>`, `%NAME%`, `process.env.NAME`, `os.environ[...]`.
_TEMPLATE_PREFIXES = (
    "env:", "env.", "${", "$(", "$", "{{", "{%", "<", "%", "process.env", "os.environ",
    "getenv(", "secret:", "vault:", "op://", "file:",
)
# Literals people send when a variable resolved to nothing.
_EMPTY_LITERALS = frozenset({
    "undefined", "null", "none", "nil", "nan", "true", "false", "bearer", "token",
    "api_key", "apikey", "api-key", "x-agent-identity", "todo", "tbd", "xxx", "***",
})
_PLACEHOLDER_WORDS = re.compile(
    r"(your[_\- ]?(api[_\- ]?)?(key|token)|replace[_\- ]?me|change[_\- ]?me|insert[_\- ]?(key|token)"
    r"|<[a-z_\- ]*(key|token)[a-z_\- ]*>|example[_\- ]?(key|token)|put[_\- ]?(your|the)[_\- ]?(key|token))",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class KeyStatus:
    state: str
    reason: str = ""
    hint: str = ""
    agent_id: Optional[str] = None
    principal_type: Optional[str] = None      # "system" | "human" | None, as usage telemetry expects

    @property
    def present(self) -> bool:
        return self.state != KEY_NONE

    @property
    def is_problem(self) -> bool:
        return self.state in PROBLEM_STATES


NO_KEY = KeyStatus(KEY_NONE)


def _looks_like_placeholder(value: str) -> Optional[str]:
    """A reason code when `value` is clearly a template/empty literal rather than a key, else None."""
    lowered = value.strip().lower()
    if _KEY_SHAPE.match(value.strip()):
        return None
    if lowered in _EMPTY_LITERALS:
        return "empty_literal"
    for p in _TEMPLATE_PREFIXES:
        if lowered.startswith(p):
            return "unexpanded_template"
    if "{{" in lowered or "${" in lowered:
        return "unexpanded_template"
    if _PLACEHOLDER_WORDS.search(value):
        return "placeholder_text"
    return None


def _free_key_url() -> str:
    base = os.getenv("PUBLIC_BASE_URL", "https://api.hatchloop.dev").rstrip("/")
    return f"{base}/keys/request"


def classify_key(raw_token: Optional[str]) -> KeyStatus:
    """Classify whatever the caller put in X-Agent-Identity / Authorization / X-Api-Key."""
    try:
        return _classify_key(raw_token)
    except Exception:  # noqa: BLE001 - a diagnostic must never break the request it describes
        return KeyStatus(KEY_INVALID, "unclassifiable",
                         "The key could not be read. Request a fresh one at " + _free_key_url() + ".")


def _classify_key(raw_token: Optional[str]) -> KeyStatus:
    if raw_token is None:
        return NO_KEY
    raw = str(raw_token).strip()
    if raw == "" or raw == "anonymous":
        return NO_KEY

    if raw.lower().startswith("bearer "):
        return KeyStatus(
            KEY_INVALID, "bearer_prefix_in_header",
            "The header value starts with 'Bearer '. Send the bare key in X-Agent-Identity, or send "
            "'Authorization: Bearer <key>' - not the word Bearer inside X-Agent-Identity.")

    placeholder = _looks_like_placeholder(raw)
    if placeholder:
        return KeyStatus(
            KEY_PLACEHOLDER, placeholder,
            "The key header holds a placeholder, not a key - a template such as env:NAME or ${NAME} "
            "that your client never expanded, or an empty variable. Check that the environment "
            "variable is set where your MCP client runs and that the client substitutes it. "
            "Need a key? Free, email-verified: " + _free_key_url())

    from agent_interface.identity import validate_token

    result = validate_token(raw)
    if result.valid and result.identity:
        principal_type = None
        try:
            principal = result.identity.principal
            if principal:
                kind = getattr(principal.kind, "value", principal.kind)
                principal_type = {"business": "system", "consumer": "human"}.get(kind)
        except Exception:  # noqa: BLE001
            principal_type = None
        return KeyStatus(KEY_VALID, "", "", agent_id=result.identity.agent_id,
                         principal_type=principal_type)

    error = (result.error or "").lower()
    if "expired" in error:
        return KeyStatus(
            KEY_EXPIRED, "expired",
            "This key has expired. Free keys last 90 days: request a new one at " + _free_key_url()
            + " (same email address).")
    if "revoked" in error:
        return KeyStatus(
            KEY_INVALID, "revoked",
            "This key has been revoked (for example after a refund). Contact support@hatchloop.dev "
            "or request a new key at " + _free_key_url() + ".")
    if "malformed" in error:
        return KeyStatus(
            KEY_INVALID, "malformed",
            "This does not have the shape of a key (<payload>.<signature>). It may have been "
            "truncated, wrapped in quotes, or split across lines. Copy the key again from the email "
            "or portal; or request one at " + _free_key_url() + ".")
    if "signature" in error:
        return KeyStatus(
            KEY_INVALID, "bad_signature",
            "The key's signature does not verify - it was altered, or it was issued by a different "
            "service. Copy the key again exactly as issued, or request a new one at "
            + _free_key_url() + ".")
    return KeyStatus(
        KEY_INVALID, "rejected",
        "The key was not accepted. Request a fresh one at " + _free_key_url() + ".")


def auth_warning(status: KeyStatus) -> Optional[dict]:
    """The block attached to results when a key was presented and failed; None otherwise.

    Wording is deliberate: it says plainly that the call was NOT treated as authenticated, says
    what still works (reads that need no key), says what will fail, and gives the fix. It carries
    the reason code, never the presented value.
    """
    if not status.is_problem:
        return None
    return {
        "key_state": status.state,
        "reason": status.reason,
        "message": (
            f"Your key was not accepted ({status.state}: {status.reason}), so this request was "
            "handled as ANONYMOUS. Tools that need no key still work; tools that need a key will "
            "answer auth_required until the key is fixed. " + status.hint
        ),
        "how_to_fix": {
            "header": "X-Agent-Identity",
            "get_a_free_key": _free_key_url(),
            "docs": "https://hatchloop.dev/agents.md",
        },
    }
