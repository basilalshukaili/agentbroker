"""
compliance_receipt -- a portable, tamper-evident evidence record for a
compliance decision this service made.

WHY THIS EXISTS

    check_compliance and screen_sanctions each hand back a DECISION. The buyer
    is the agent OPERATOR, and the operator is the party carrying the legal
    liability for what their agent did. What that operator needs from us is not
    our opinion at query time -- it is a record they still hold months later
    that states WHAT was screened, against WHICH copy of which data, under
    WHICH decision code, at WHAT instant, and WHICH answer came back, and that
    shows whether it has been altered since. Sanctions recordkeeping duties run
    for years, and the EU AI Act puts the logging duty on the operator's own
    system rather than on ours, so the artefact has to end up in their hands.

    That is the whole feature: turning "we checked" into "here is the record of
    the check", in a form the customer keeps and can produce.

    THERE IS DELIBERATELY NO STORAGE HERE. We build the receipt, sign it if we
    can, hand it over, and keep nothing. A copy we retain is a copy we can
    lose, corrupt or be compelled to produce, and holding it would make us the
    custodian of the customer's evidence -- which is the position they are
    paying to be out of. If retention is ever wanted it belongs behind an
    explicit opt-in with its own retention policy, not as a side effect of
    calling a free read tool.

WHAT A RECEIPT ASSERTS, AND THE LINE IT DOES NOT CROSS

    Every field is a fact about OUR OWN SYSTEM: what we screened, which copy of
    which list, how old that copy was, which decision code ran, when, and what
    we returned. Nothing here asserts a fact about the world.

    A receipt NEVER says "this party is not sanctioned". It says "screened
    against the OFAC SDN copy in hand and the EU index refreshed on 2026-08-30,
    and no confirmed match was returned". Those are different claims and only
    the second one is ours to make. `payload.does_not_assert` carries that
    difference INSIDE the record, so the limits travel with the evidence
    instead of living in documentation an auditor will never be shown.

INTEGRITY, AND THE FAILURE MODE THIS FILE EXISTS TO PREVENT

    A receipt that claims to be signed when it is not would be worse than no
    receipt at all: the customer would hand a court an artefact whose central
    claim about itself is false, and they would find out at the worst possible
    moment. So the signature block is explicit in both directions and never
    optimistic:

      * key configured   -> signature_status "signed", Ed25519 over the exact
                            canonical bytes, with the key id and public key.
      * no key           -> signature_status "unsigned", signature null, and a
                            `what_this_proves` sentence that says in plain
                            words that the hash alone does not prove origin and
                            can be recomputed by anyone who edits the payload.
      * key set but junk -> signature_status "unsigned" with reason
                            "signing_key_misconfigured", surfaced in EVERY
                            receipt. Loud, visible to the customer, and still
                            not fatal to a free read tool that was working
                            fine before someone typo'd an environment variable.

    Ed25519 and not the HMAC used by billing/receipt_signer.py, and the reason
    is the whole point of the feature. HMAC verification needs the signing
    secret, so we could only ever verify it ourselves -- either the customer
    calls us back (the record is not in their hands) or we ship them a key that
    also lets them forge (the record proves nothing). An asymmetric signature
    is the only shape where the customer verifies offline, alone, forever, with
    something we can publish. billing's HMAC is correct for what it does: it
    guards OUR billing integrity, and both ends of that check are us.

    WHAT A VALID SIGNATURE DOES NOT PROVE. The receipt embeds its own public
    key so it is self-contained, but a receipt that verifies against the key it
    carries proves only INTERNAL CONSISTENCY -- anyone can generate a keypair,
    sign a fabricated payload, and embed the matching public key. Proving the
    receipt came from AgentBroker requires checking it against a public key
    obtained from us out of band. `verify_compliance_receipt` reports that as a
    separate field (`origin_proven`) rather than folding it into one boolean,
    because the two questions have different answers and the difference is
    exactly what an auditor is asking about.

CANONICALIZATION

    The signed bytes are `canonical_bytes(payload)`:

        json.dumps(payload, sort_keys=True, separators=(",", ":"),
                   ensure_ascii=True, allow_nan=False).encode("utf-8")

    Key order is fixed by sorting, whitespace is eliminated, and ensure_ascii
    keeps the byte stream pure ASCII so an independent verifier in another
    language does not have to agree with us about Unicode normalisation. The
    receipt states this string in `payload.canonical_form`, so a customer can
    reimplement the check without our source.

    ONE HONEST CAVEAT, stated here rather than discovered later: floating-point
    numbers are emitted as the shortest decimal that round-trips (Python repr,
    which agrees with ECMAScript Number::toString for ordinary magnitudes but
    not for very large or very small exponents). The receipt payload itself
    contains no floats for that reason -- every number in it is an integer or a
    string. The caveat only reaches `response_sha256`, which is taken over the
    tool's own result payload, and that payload does carry match scores. A
    verifier written in Python (including the one in this file) is exact; a
    hand-rolled verifier in another language should compare `response_sha256`
    using the same shortest-round-trip float formatting.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger("smb_broker.compliance_receipt")

# The key an emitted receipt occupies inside a tool's `result` payload. Named
# once here because the response-binding hash is defined as "the result payload
# with THIS key removed", and two spellings of it would silently break every
# verification.
RECEIPT_FIELD = "compliance_receipt"

RECEIPT_VERSION = "agentbroker-compliance-receipt/1"

CANONICAL_FORM = (
    'json.dumps(payload, sort_keys=True, separators=(",", ":"), '
    'ensure_ascii=True, allow_nan=False).encode("utf-8")'
)

ISSUER = {
    "service": "AgentBroker",
    "domain": "hatchloop.dev",
}

_SIGNING_KEY_ENV = "COMPLIANCE_RECEIPT_SIGNING_KEY"
_KEY_ID_ENV = "COMPLIANCE_RECEIPT_KEY_ID"


# ---------------------------------------------------------------------------
# Canonicalization and hashing
# ---------------------------------------------------------------------------

def _json_default(obj: Any) -> Any:
    """Coerce the few non-JSON types that reach us into stable scalars.

    Enums first: JurisdictionRules carries a str-Enum, and while a str-Enum
    happens to serialise as its value, an int- or plain Enum would not, and a
    canonical form that depends on which Enum base someone picked is not
    canonical.
    """
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, datetime):
        return obj.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(obj, (set, frozenset)):
        return sorted(obj)
    if isinstance(obj, bytes):
        return obj.decode("utf-8", "replace")
    # Last resort. Call sites pass primitives, dicts and lists; anything else
    # arriving here is a bug, and str() of an object with the default __repr__
    # embeds a memory address, which makes two receipts for identical calls
    # differ. Verification still holds (the string is in the payload that was
    # hashed), but treat this branch appearing in a receipt as a defect.
    return str(obj)


def canonical_bytes(obj: Any) -> bytes:
    """The exact byte stream that is hashed and signed.

    allow_nan=False on purpose: NaN and Infinity are not JSON, every language
    disagrees about how to write them, and a receipt whose bytes depend on the
    reader's JSON dialect cannot be verified. Failing here is right -- the
    caller wraps receipt building so a refusal costs the customer a receipt,
    never the answer they asked for.
    """
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
        default=_json_default,
    ).encode("utf-8")


def sha256_of(obj: Any) -> str:
    """`sha256:<hex>` over the canonical bytes of `obj`."""
    return "sha256:" + hashlib.sha256(canonical_bytes(obj)).hexdigest()


def sha256_text(text: str) -> str:
    """`sha256:<hex>` over the raw UTF-8 bytes of a string.

    Deliberately NOT sha256_of(), which would hash the JSON-quoted form. This
    one is what a customer gets from `sha256sum message.txt` or
    `crypto.createHash('sha256').update(body)`, and a digest they cannot
    reproduce with the obvious tool is a digest they will not trust.
    """
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Signing key
# ---------------------------------------------------------------------------

# Cached on the RAW ENV STRING rather than loaded once at import.
#
# billing/receipt_signer.py reads its key at import time, which is fine for a
# process-lifetime secret but makes the behaviour untestable without reloading
# the module -- and a test that has to reload a module to change a key is a
# test people stop writing. Re-reading os.environ costs a dict lookup; the
# expensive part (parsing the seed, deriving the public key) still happens only
# when the value actually changes.
_key_cache: dict[str, Any] = {"raw": None, "state": None}


def _decode_seed(raw: str) -> Optional[bytes]:
    """A 32-byte Ed25519 seed from hex or base64. None if it is neither."""
    import base64
    import binascii

    candidate = raw.strip()
    try:
        seed = bytes.fromhex(candidate)
        if len(seed) == 32:
            return seed
    except (ValueError, binascii.Error):
        pass
    try:
        seed = base64.b64decode(candidate, validate=True)
        if len(seed) == 32:
            return seed
    except (ValueError, binascii.Error):
        pass
    return None


def _load_key() -> dict:
    """Return the current signing state.

    Shape: {"mode": "signed"|"unsigned", "reason": str|None,
            "private": key|None, "public_hex": str|None, "key_id": str|None}

    NEVER raises. A misconfigured key downgrades the receipt and says so; it
    does not take a free, read-only compliance tool off the air.
    """
    raw = os.environ.get(_SIGNING_KEY_ENV, "") or ""
    if _key_cache["raw"] == raw and _key_cache["state"] is not None:
        return _key_cache["state"]

    state: dict[str, Any]
    if not raw.strip():
        state = {"mode": "unsigned", "reason": "no_signing_key_configured",
                 "private": None, "public_hex": None, "key_id": None}
    else:
        seed = _decode_seed(raw)
        if seed is None:
            # LOUD, IN EVERY RECEIPT, AND STILL NOT FATAL. An operator who
            # typo'd the secret must find out from the artefact rather than
            # from a customer's lawyer, but a bad env var must not turn a
            # working $0 screening tool into an outage.
            logger.error(
                "%s is set but is not a 32-byte Ed25519 seed in hex or base64; "
                "compliance receipts will be issued UNSIGNED", _SIGNING_KEY_ENV)
            state = {"mode": "unsigned", "reason": "signing_key_misconfigured",
                     "private": None, "public_hex": None, "key_id": None}
        else:
            try:
                from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                    Ed25519PrivateKey)
                from cryptography.hazmat.primitives import serialization
                private = Ed25519PrivateKey.from_private_bytes(seed)
                public_hex = private.public_key().public_bytes(
                    encoding=serialization.Encoding.Raw,
                    format=serialization.PublicFormat.Raw,
                ).hex()
                # DERIVED, NOT INVENTED. With no key id configured we publish a
                # deterministic function of the public key, so two deployments
                # holding the same key produce the same id and nobody has to
                # trust a hand-typed label to line receipts up with a key.
                key_id = os.environ.get(_KEY_ID_ENV, "").strip() or (
                    "ed25519-" + hashlib.sha256(
                        bytes.fromhex(public_hex)).hexdigest()[:16])
                state = {"mode": "signed", "reason": None, "private": private,
                         "public_hex": public_hex, "key_id": key_id}
            except Exception as exc:            # noqa: BLE001
                logger.error(
                    "compliance receipt signing unavailable (%s); receipts will "
                    "be issued UNSIGNED", exc)
                state = {"mode": "unsigned",
                         "reason": "signing_backend_unavailable",
                         "private": None, "public_hex": None, "key_id": None}

    _key_cache["raw"] = raw
    _key_cache["state"] = state
    return state


def signing_key_status() -> dict:
    """Diagnostics for operators: is signing on, and if not, why not.

    Deliberately carries no secret material -- the public key is public and the
    reason codes are the same ones printed in every receipt.
    """
    st = _load_key()
    return {
        "signature_status": "signed" if st["mode"] == "signed" else "unsigned",
        "reason": st["reason"],
        "key_id": st["key_id"],
        "public_key_ed25519_hex": st["public_hex"],
        "env_var": _SIGNING_KEY_ENV,
    }


_UNSIGNED_MEANING = (
    "NOT SIGNED. No signing key is configured on the issuing service, so this "
    "receipt carries an integrity hash only. The hash binds these fields to "
    "each other and to the response, and detects accidental corruption or a "
    "partial copy - it does NOT prove the receipt came from AgentBroker, and "
    "anyone who edits the payload can recompute it. Treat this as a "
    "self-recorded log entry, not as evidence against a third party."
)

_MISCONFIGURED_MEANING = (
    "NOT SIGNED, because the issuing service's signing key is misconfigured. "
    "This is a fault on our side, not a property of your check: the decision "
    "recorded here ran normally. The receipt carries an integrity hash only, "
    "which detects corruption but does not prove origin. Contact the issuer "
    "for a signed reissue policy before relying on this as evidence."
)

_SIGNED_MEANING = (
    "Signed with Ed25519 over the canonical bytes of `payload`. Anyone holding "
    "AgentBroker's public key for this key_id can confirm offline that no "
    "field of `payload` has changed since issue. NOTE: verifying against the "
    "public_key embedded here proves internal consistency only - a forger can "
    "sign anything with their own keypair and embed their own public key. To "
    "prove ORIGIN, verify against a public key you obtained from AgentBroker "
    "out of band and compare it to public_key below."
)


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------

def _integrity_block(payload: dict) -> dict:
    body = canonical_bytes(payload)
    # The canonical form is stated ONCE, inside `payload`, where the signature
    # covers it. A second copy out here would be an unsigned field describing
    # how the signed field is hashed - editable, and free to disagree with the
    # one that counts.
    block: dict[str, Any] = {
        "payload_sha256": "sha256:" + hashlib.sha256(body).hexdigest(),
        "signature_algorithm": None,
        "signature": None,
        "key_id": None,
        "public_key_ed25519_hex": None,
    }
    st = _load_key()
    if st["mode"] == "signed":
        try:
            block["signature"] = st["private"].sign(body).hex()
            block["signature_status"] = "signed"
            block["signature_algorithm"] = "ed25519"
            block["key_id"] = st["key_id"]
            block["public_key_ed25519_hex"] = st["public_hex"]
            block["what_this_proves"] = _SIGNED_MEANING
            return block
        except Exception as exc:                # noqa: BLE001
            # A signer that fails at signing time must not produce a receipt
            # that still calls itself signed. Fall through to the unsigned
            # block with a reason, which is the whole discipline of this file.
            logger.error("compliance receipt signing failed: %s", exc)
            block["signature_status"] = "unsigned"
            block["unsigned_reason"] = "signing_failed"
            block["what_this_proves"] = _MISCONFIGURED_MEANING
            return block

    block["signature_status"] = "unsigned"
    block["unsigned_reason"] = st["reason"]
    block["what_this_proves"] = (
        _MISCONFIGURED_MEANING
        if st["reason"] in ("signing_key_misconfigured", "signing_backend_unavailable")
        else _UNSIGNED_MEANING)
    return block


def build_receipt(
    *,
    tool: str,
    operation_id: str,
    subject: dict,
    inputs: dict,
    evidence: dict,
    asserts: str,
    does_not_assert: list[str],
    response_payload: Optional[dict] = None,
    issued_at: Optional[str] = None,
    service_version: Optional[str] = None,
) -> dict:
    """Build one receipt. Pure apart from reading the clock and the key.

    `response_payload` is the tool's own `result` dict. The binding hash is
    taken with RECEIPT_FIELD removed, because the receipt cannot contain a hash
    of a payload that contains the receipt. Passing the payload BEFORE the
    receipt is inserted gives the same bytes and is what the call sites do.
    """
    payload = {
        "receipt_version": RECEIPT_VERSION,
        "issuer": {**ISSUER, "service_version": service_version or "unknown"},
        "tool": tool,
        "operation_id": operation_id,
        "issued_at": issued_at or datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"),
        "asserts": asserts,
        "does_not_assert": list(does_not_assert),
        "subject": subject,
        "inputs_sha256": sha256_of(inputs),
        # A digest nobody can reproduce is decoration. Say exactly what was
        # hashed, since "the inputs" is ambiguous between what the caller sent
        # and what the handler resolved them to.
        "inputs_binding": (
            "sha256 over the canonical bytes of the RESOLVED tool arguments "
            "(after defaults and inference), keyed by the tool's own parameter "
            "names, using canonical_form below."),
        "evidence": evidence,
        "canonical_form": CANONICAL_FORM,
    }
    if response_payload is not None:
        bound = {k: v for k, v in response_payload.items() if k != RECEIPT_FIELD}
        payload["response_sha256"] = sha256_of(bound)
        payload["response_binding"] = (
            f"sha256 over the canonical bytes of this call's result payload "
            f"with the '{RECEIPT_FIELD}' key removed. Recompute it over the "
            f"response you stored to prove the receipt and the answer belong "
            f"together.")

    return {"payload": payload, "integrity": _integrity_block(payload)}


def attach_receipt(result_payload: dict, **kwargs: Any) -> None:
    """Build a receipt and put it in `result_payload[RECEIPT_FIELD]`.

    NEVER RAISES, and never modifies anything else. The receipt is an addition
    to an answer the caller already has; a defect in evidence generation must
    not turn a working, free, read-only compliance check into a failure or a
    charge. A receipt that cannot be built is simply absent, and absence
    asserts nothing.
    """
    try:
        result_payload[RECEIPT_FIELD] = build_receipt(
            response_payload=result_payload, **kwargs)
    except Exception as exc:                    # noqa: BLE001
        logger.warning("compliance receipt not issued (%s): %s",
                       kwargs.get("tool"), exc)


def service_version() -> str:
    """The manifest version this build advertises, or 'unknown'.

    Never guesses. If the manifest cannot be read the receipt says unknown,
    which is a true statement about what we know; a made-up version number
    would be a false one about what code ran.
    """
    try:
        from agent_interface.manifest_server import get_manifest_version
        v = get_manifest_version()
        got = v.get("version") if isinstance(v, dict) else None
        return str(got) if got else "unknown"
    except Exception:                           # noqa: BLE001
        return "unknown"


_source_fingerprints: dict[str, str] = {}


def source_fingerprint(module: Any) -> str:
    """`sha256:<hex>` of a module's source, or 'unavailable'.

    WHY A SOURCE HASH RATHER THAN A VERSION STRING. The compliance gate has no
    version number, and inventing one would mean maintaining a constant that
    nothing forces anyone to bump - the exact "a sentence left behind by code
    that changed underneath it" failure the rest of this codebase is full of
    fixes for. A hash of the code that actually ran cannot drift: two receipts
    carrying the same fingerprint were produced by identical decision logic.
    It is conservative in the safe direction - a comment edit changes it, so it
    can over-report a change and can never under-report one.

    Cached for the life of the process: the source of a loaded module does not
    change under it, and re-reading and re-hashing a 400-line file on every
    call to a free sub-100ms tool is latency spent on a constant. Measured, it
    was most of the cost of issuing a receipt.
    """
    name = getattr(module, "__name__", None) or str(module)
    hit = _source_fingerprints.get(name)
    if hit is not None:
        return hit
    try:
        import inspect
        src = inspect.getsource(module)
        out = "sha256:" + hashlib.sha256(src.encode("utf-8")).hexdigest()
    except Exception:                           # noqa: BLE001
        # NOT CACHED. "unavailable" is a fact about this attempt, and caching
        # it would pin an unknown fingerprint into every later receipt after a
        # single transient read failure.
        return "unavailable"
    _source_fingerprints[name] = out
    return out


# ---------------------------------------------------------------------------
# Verification -- the half that lives in the customer's hands
# ---------------------------------------------------------------------------

def verify_compliance_receipt(
    receipt: Any,
    *,
    expected_public_key_hex: Optional[str] = None,
    response_payload: Optional[dict] = None,
) -> dict:
    """Check a receipt offline. Pure: no network, no database, no clock.

    Returns a dict, never a bare bool, because "is this receipt good" is four
    questions with four different answers and collapsing them into one boolean
    is how an unsigned record gets read as a signed one:

        verdict          verified_signed | verified_unsigned_hash_only
                         | tampered | signature_invalid | malformed
                         | cannot_verify_no_crypto_library
        hash_ok          the payload still hashes to payload_sha256
        signature_status valid | invalid | absent | unverifiable
        origin_proven    True ONLY when `expected_public_key_hex` was supplied
                         and the signature verified against it. A signature
                         that verifies against the key the receipt carries
                         proves internal consistency, not who issued it.
        response_match   True/False/None - whether `response_payload` is the
                         answer this receipt was issued for (None if not given)
        tamper_evident   whether this receipt can detect a deliberate edit at
                         all. False for unsigned receipts: their hash is
                         recomputable by whoever edited them.

    `expected_public_key_hex` is how a customer pins us: pass the AgentBroker
    public key you were given out of band and origin_proven becomes meaningful.
    """
    reasons: list[str] = []

    def _out(verdict: str, **kw: Any) -> dict:
        base = {
            "verdict": verdict,
            "hash_ok": None,
            "signature_status": "absent",
            "origin_proven": False,
            "response_match": None,
            "tamper_evident": False,
            "key_id": None,
            "reasons": reasons,
        }
        base.update(kw)
        base["human_message"] = _VERDICT_TEXT.get(verdict, verdict)
        return base

    if not isinstance(receipt, dict):
        reasons.append("receipt is not an object")
        return _out("malformed")
    payload = receipt.get("payload")
    integrity = receipt.get("integrity")
    if not isinstance(payload, dict) or not isinstance(integrity, dict):
        reasons.append("receipt must have object 'payload' and 'integrity' keys")
        return _out("malformed")

    claimed_hash = integrity.get("payload_sha256")
    if not isinstance(claimed_hash, str) or not claimed_hash.startswith("sha256:"):
        reasons.append("integrity.payload_sha256 missing or not a sha256:<hex>")
        return _out("malformed")

    try:
        body = canonical_bytes(payload)
    except Exception as exc:                    # noqa: BLE001
        reasons.append(f"payload is not canonicalizable: {exc}")
        return _out("malformed")

    actual_hash = "sha256:" + hashlib.sha256(body).hexdigest()
    # Constant-time compare: these are public digests so timing is not a real
    # attack here, but the habit is cheap and the wrong habit is not.
    import hmac as _hmac
    hash_ok = _hmac.compare_digest(actual_hash, claimed_hash)
    if not hash_ok:
        reasons.append(
            "payload does not hash to integrity.payload_sha256 - the record "
            "has been altered or truncated since it was issued")

    # --- response binding -------------------------------------------------
    response_match: Optional[bool] = None
    if response_payload is not None:
        bound = {k: v for k, v in response_payload.items() if k != RECEIPT_FIELD}
        try:
            response_match = _hmac.compare_digest(
                sha256_of(bound), str(payload.get("response_sha256") or ""))
        except Exception as exc:                # noqa: BLE001
            reasons.append(f"response payload is not canonicalizable: {exc}")
            response_match = False
        if response_match is False:
            reasons.append(
                "the response payload supplied is not the one this receipt was "
                "issued for (response_sha256 mismatch)")

    key_id = integrity.get("key_id")
    sig_hex = integrity.get("signature")
    claimed_status = integrity.get("signature_status")

    # --- unsigned ---------------------------------------------------------
    if not sig_hex:
        if claimed_status == "signed":
            # A receipt whose own block says "signed" with nothing to verify is
            # the exact lie this feature exists to make impossible. Refuse it
            # rather than downgrading it quietly to "unsigned but fine".
            reasons.append(
                "integrity.signature_status says 'signed' but no signature is "
                "present")
            return _out("malformed", hash_ok=hash_ok,
                        response_match=response_match)
        reasons.append(
            "receipt is unsigned: the hash detects corruption but is "
            "recomputable, so it does not prove origin")
        if expected_public_key_hex:
            # The caller came to prove origin and there is nothing to check.
            # Saying only "unsigned" would leave them to notice that their pin
            # was silently ignored.
            reasons.append(
                "a public key was pinned, but this receipt carries no "
                "signature to check against it - origin cannot be proven")
        return _out("tampered" if not hash_ok else "verified_unsigned_hash_only",
                    hash_ok=hash_ok, signature_status="absent",
                    response_match=response_match, tamper_evident=False,
                    key_id=key_id)

    # --- signed -----------------------------------------------------------
    embedded_pub = integrity.get("public_key_ed25519_hex")
    pub_hex = expected_public_key_hex or embedded_pub
    if not isinstance(pub_hex, str) or not pub_hex:
        reasons.append("a signature is present but no public key to check it with")
        return _out("malformed", hash_ok=hash_ok, response_match=response_match)

    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey)
        from cryptography.exceptions import InvalidSignature
    except Exception:                           # noqa: BLE001
        # HONEST DEGRADATION ON THE VERIFIER SIDE TOO. "I do not have the
        # library to check this" is not "this is fine", and a customer running
        # the offline verifier on a bare machine must not be told it passed.
        reasons.append(
            "the 'cryptography' package is not installed, so the Ed25519 "
            "signature could not be checked (pip install cryptography)")
        return _out("cannot_verify_no_crypto_library", hash_ok=hash_ok,
                    signature_status="unverifiable",
                    response_match=response_match, tamper_evident=True,
                    key_id=key_id)

    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(pub_hex)).verify(
            bytes.fromhex(str(sig_hex)), body)
        sig_ok = True
    except InvalidSignature:
        sig_ok = False
    except Exception as exc:                    # noqa: BLE001
        reasons.append(f"signature or public key is malformed: {exc}")
        return _out("malformed", hash_ok=hash_ok, response_match=response_match)

    if not sig_ok:
        reasons.append(
            "the Ed25519 signature does not match this payload and public key "
            "- the record has been altered, or it was signed by a different key")
        return _out("tampered" if not hash_ok else "signature_invalid",
                    hash_ok=hash_ok, signature_status="invalid",
                    response_match=response_match, tamper_evident=True,
                    key_id=key_id)

    if not hash_ok:
        # THE SIGNATURE IS OVER `payload`, NOT OVER THE INTEGRITY BLOCK, so a
        # valid signature with a wrong declared hash means someone edited the
        # record's own integrity block. The payload is authentic and the
        # document is not, and the verdict has to be the second one - reporting
        # "verified_signed" with hash_ok:false hands the reader a headline that
        # contradicts the field under it.
        reasons.append(
            "the signature is valid over the payload, but "
            "integrity.payload_sha256 does not match it - the integrity block "
            "has been edited")
        return _out("tampered", hash_ok=False, signature_status="valid",
                    response_match=response_match, tamper_evident=True,
                    key_id=key_id)

    origin_proven = bool(expected_public_key_hex)
    if not origin_proven:
        reasons.append(
            "verified against the public key embedded in the receipt, which "
            "proves internal consistency only. Pass expected_public_key_hex "
            "with a key obtained from the issuer out of band to prove origin.")
    return _out("verified_signed", hash_ok=hash_ok, signature_status="valid",
                origin_proven=origin_proven, response_match=response_match,
                tamper_evident=True, key_id=key_id)


_VERDICT_TEXT = {
    "verified_signed":
        "Signature valid and the payload is unchanged since it was issued.",
    "verified_unsigned_hash_only":
        "The payload matches its integrity hash, but this receipt is UNSIGNED: "
        "the hash can be recomputed by anyone who edits it, so it is not "
        "evidence of origin.",
    "tampered":
        "This receipt does not match its own integrity hash. It has been "
        "altered or truncated since it was issued.",
    "signature_invalid":
        "The payload matches its hash but the signature does not verify. The "
        "record was signed by a different key, or the signature was replaced.",
    "malformed":
        "This is not a well-formed AgentBroker compliance receipt.",
    "cannot_verify_no_crypto_library":
        "The payload matches its integrity hash, but the Ed25519 signature "
        "could not be checked on this machine - install 'cryptography' and "
        "re-run before treating this as verified.",
}


# ---------------------------------------------------------------------------
# Offline CLI: `python -m core.compliance_receipt <receipt.json> [pubkey_hex]`
# ---------------------------------------------------------------------------

def _main(argv: list[str]) -> int:
    if not argv:
        print("usage: python -m core.compliance_receipt <receipt.json> "
              "[expected_public_key_hex]")
        return 2
    with open(argv[0], encoding="utf-8") as fh:
        doc = json.load(fh)
    # Accept either a bare receipt or a whole tool result carrying one, because
    # what a customer actually saved is usually the tool's response.
    if isinstance(doc, dict) and RECEIPT_FIELD in doc:
        result = verify_compliance_receipt(
            doc[RECEIPT_FIELD],
            expected_public_key_hex=argv[1] if len(argv) > 1 else None,
            response_payload=doc)
    else:
        result = verify_compliance_receipt(
            doc, expected_public_key_hex=argv[1] if len(argv) > 1 else None)
    print(json.dumps(result, indent=2))
    return 0 if result["verdict"].startswith("verified") else 1


if __name__ == "__main__":                      # pragma: no cover
    import sys
    raise SystemExit(_main(sys.argv[1:]))
