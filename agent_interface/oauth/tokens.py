"""Secrets, digests, PKCE, and turning a verified email into the identity an access token carries.

THE ACCESS TOKEN IS AN AGENT-IDENTITY KEY. It is the same signed `<payload>.<signature>` value that
`/keys/verify` emails and the portal reveals, validated by the same `identity.validate_token`, accepted in
the same places (`Authorization: Bearer ...` is already promoted to `X-Agent-Identity` at the MCP door) and
metered by the same code. What differs is only how it was obtained and how long it lives: one hour, bound
(`aud`) to the resource it was issued for, and re-issued - with the account re-read - at every refresh.

Which account an email stands for:

  * An email that has bought credits stands for its `sub_<customer>` account (the account the Polar webhook
    credits, linked by `oauth_account_link` when the order is processed). The token carries that account as
    its agent id, so tool calls spend the credits the person bought on the website - which is where credits
    must be bought (ChatGPT's app rules forbid selling them inside the conversation).
  * Any other email stands for `free_<first 16 hex of sha256(email)>` - exactly the id the email-verified
    free key and the portal's "generate key" already derive, so signing in with the same address on any
    surface lands on the same account, the same daily allowance, the same history.

A purchase is picked up at the next refresh (within the hour) without the person doing anything; a refund
that revokes the customer (identity.revoke_customer) stops the next refresh and the token itself.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import secrets
from dataclasses import dataclass
from typing import Optional

from agent_interface.oauth import settings

log = logging.getLogger("smb_broker.oauth")

_VERIFIER = re.compile(r"^[A-Za-z0-9\-._~]{43,128}$")
_CHALLENGE = re.compile(r"^[A-Za-z0-9_-]{43}$")


def new_secret(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalise_email(email: str) -> str:
    return (email or "").strip().lower()


def email_hash(email: str) -> str:
    """The person's identity as the service stores it: sha256 of the lower-cased, trimmed address."""
    return sha256_hex(normalise_email(email))


def free_account_id(email_digest: str) -> str:
    """Identical to the id key_requests.verify_free_key and portal.key/generate derive from the address."""
    return f"free_{email_digest[:16]}"


def mask_email(email: str) -> str:
    """j***@gmail.com - enough for a person to recognise their own address on a confirmation page."""
    local, _, domain = normalise_email(email).partition("@")
    if not local or not domain:
        return "your email"
    return f"{local[0]}***@{domain}"


def match_code(request_id: str) -> str:
    """The 4-digit code the page that STARTED a sign-in shows, derived (not stored) from its id.

    A link opened somewhere other than the starting browser asks for it. It defends against the passive
    victim: someone starts a sign-in with your address, you receive a genuine email, you press Confirm. You
    have no code, so nothing is connected. It does not defend against being talked into reading a code out to
    the person who started it - nothing a link can do does."""
    mac = hmac.new(settings.state_secret().encode(), b"oauth-match|" + request_id.encode(), hashlib.sha256).digest()
    return f"{int.from_bytes(mac[:4], 'big') % 10000:04d}"


def match_code_ok(request_id: str, given: Optional[str]) -> bool:
    digits = re.sub(r"\D", "", given or "")
    return len(digits) == 4 and hmac.compare_digest(match_code(request_id), digits)


def valid_challenge(value: Optional[str]) -> bool:
    return bool(value and _CHALLENGE.match(value))


def pkce_matches(verifier: Optional[str], challenge: str) -> bool:
    """RFC 7636 S256: BASE64URL(SHA256(verifier)) == challenge, in constant time."""
    if not verifier or not _VERIFIER.match(verifier):
        return False
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    computed = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return hmac.compare_digest(computed, challenge or "")


@dataclass(frozen=True)
class Subject:
    agent_id: str
    principal_id: str
    paid: bool
    plan: str


def _paid_subject(row: dict) -> Optional[Subject]:
    account_id = str(row.get("account_id") or "")
    if not account_id.startswith("sub_"):
        return None
    customer = str(row.get("customer_id") or account_id[len("sub_"):])
    return Subject(agent_id=account_id, principal_id=customer, paid=True,
                   plan=str(row.get("plan") or "developer").lower())


async def resolve_subject(store, email_digest: str) -> Optional[Subject]:
    """The identity to put in a token for this email, or None when the account has been revoked.

    A lookup that FAILS falls back to the free identity and says so loudly: refusing a sign-in because the
    account lookup blipped would lock a paying customer out of their assistant, while the cost of the
    fallback is bounded (a free allowance until the next refresh, within the hour)."""
    row = None
    try:
        row = await store.account_for_email(email_digest)
    except Exception as exc:  # noqa: BLE001
        log.warning("oauth_account_lookup_failed err=%s -- minting the free identity; the next refresh "
                    "re-reads the account", type(exc).__name__)
    if row:
        subject = _paid_subject(row)
        if subject is not None:
            from agent_interface.identity import paid_customer_revoked
            if paid_customer_revoked(subject.principal_id):
                return None
            return subject
    free = free_account_id(email_digest)
    return Subject(agent_id=free, principal_id=free, paid=False, plan="free")


def mint_access_token(subject: Subject, *, resource: str, scope: str, client_id: str, family_id: str):
    """A signed, short-lived Agent-Identity token for `subject`, bound to `resource`."""
    from agent_interface import identity as ident

    if subject.paid:
        ops, cap, verticals, _ttl = ident._PLAN_SCOPES.get(subject.plan, ident._PLAN_SCOPES["developer"])
    else:
        ops, cap, verticals = ["*"], 0.0, ["*"]       # the free key's scope: no credit spend, daily allowance
    return ident.issue_token(ident.TokenRequest(
        agent_id=subject.agent_id,
        principal_id=subject.principal_id,
        principal_type="human",
        allowed_operations=list(ops),
        budget_cap_usd=cap,
        allowed_verticals=list(verticals),
        ttl_seconds=settings.ACCESS_TTL_S,
        extra_claims={
            "aud": resource,
            "scp": scope,
            "oauth": {"cid": sha256_hex(client_id)[:16], "gid": family_id[:32]},
        },
    ))
