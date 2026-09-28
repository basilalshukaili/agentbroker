"""
Durable, per-contact opt-out check for compliance/pre_check.py.

WHY THIS FILE EXISTS. consent_optouts has RLS enabled with zero policies, and
this container holds only the anon key, which does not bypass RLS. A bulk
read of that table (main.py's boot-time hydrate_opted_out) gets HTTP 200 with
an empty array -- not an error -- so it silently loads ZERO durable opt-outs
on every boot, and the in-memory enforcement set in compliance/consent_store.py
behaves as though nobody has ever opted out. Reintroduces the exact bug
tests/compliance_tests/test_optout_enforcement.py was written to prevent,
through RLS instead of the original no-op-on-no-prior-opt-in bug.

A bulk-read fix cannot be made safe for the anon key: consent_optouts stores
phone numbers and email addresses in plaintext, and there is no per-call
scoping key for "give me every opt-out" the way there is for "give me the
lead with this dedup_key". See sql/agentbroker/006_consent_optouts_membership_rpc.sql
for the full analysis. Instead, this module asks a narrower question per send:
"has THIS ONE contact opted out?" -- via consent_optouts_is_opted_out, a
SECURITY DEFINER Postgres function that bypasses RLS and returns ONLY a
boolean, never a row, a count, or a timestamp.

THE GOVERNING RULE, copied from core/screen_sanctions.py's "AN EMPTY INDEX IS
NOT A CLEAN SCREEN" guard because it is the identical shape applied to a
different table: "no row matched" and "I could not check" are different
answers and must never collapse into each other. check_durable_optout()
raises OptoutCheckUnavailable for every failure mode except one deliberate
exception -- see its docstring -- and compliance/pre_check.py treats that
exception as a reason to refuse the send, never as "not opted out".
"""
from __future__ import annotations

import logging

logger = logging.getLogger("smb_broker.optout_gate")

_RPC_NAME = "consent_optouts_is_opted_out"


class OptoutCheckUnavailable(Exception):
    """The durable opt-out record could not be consulted for this contact.

    NEVER treat this as "the contact is not opted out". The only safe
    response is to refuse the send and let the caller retry.
    """


def check_durable_optout(recipient_id: str) -> bool:
    """Ask the durable store, authoritatively, whether `recipient_id` has
    opted out -- on ANY channel, matching compliance/consent_store.py's
    ConsentStore.is_opted_out() widening (a STOP suppresses the contact, not
    just the transport it arrived on).

    Returns False -- durable check skipped, in-memory state is the whole
    picture -- ONLY when Supabase is not configured at all (no SUPABASE_URL /
    key). That is a deliberate, narrow exception, not a loophole: it is the
    same "NOT CONFIGURED IS NOT THE SAME AS DOWN" posture
    agent_interface/unsubscribe.py already documents for the write side of
    this exact table -- a container with no Supabase credentials cannot be
    the live production regression this file exists to close, because
    production always has them configured (billing and leads already require
    it to do anything). Local dev and the test suite run with no Supabase
    configured, and in-memory-only suppression is the honest, intended
    behaviour there.

    Raises OptoutCheckUnavailable for every OTHER failure: a configured-but-
    unreachable database, a non-200, a response that will not parse as JSON,
    or an RPC that stops returning a plain boolean (a schema drift, a bad
    deploy, or a future edit that quietly turns this membership test back
    into something row-shaped -- defended against explicitly, since that is
    exactly the enumeration risk this design exists to avoid).
    """
    from storage.supabase_client import _get_config, rpc_sync

    url, key = _get_config()
    if not url or not key:
        logger.debug("optout_check_skipped reason=missing_config")
        return False

    try:
        result = rpc_sync(_RPC_NAME, {"p_recipient_id": recipient_id})
    except Exception as exc:  # noqa: BLE001
        raise OptoutCheckUnavailable(
            f"{_RPC_NAME} failed: {exc}"
        ) from exc

    # STRICT SHAPE CHECK. The whole point of this RPC is that it returns
    # ONLY a boolean -- never a row, a list, or anything a caller could later
    # be tempted to read fields off of. If it ever comes back as anything
    # else, that is not "opted out" and it is not "not opted out" -- it is a
    # broken contract, and the only safe reading of a broken contract on this
    # path is "could not check".
    if isinstance(result, bool):
        return result

    raise OptoutCheckUnavailable(
        f"{_RPC_NAME} returned {type(result).__name__}, not a boolean -- "
        f"refusing to trust it (never logging the value: it may be PII)"
    )
