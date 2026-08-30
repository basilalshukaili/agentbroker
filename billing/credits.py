"""
billing/credits.py -- credits rail for AgentBroker.

The credits rail is the second billing path (alongside x402). Agents that
authenticate via X-Agent-Identity with a funded credit_accounts row use this
path instead of x402; the x402 branch in the dispatcher checks for the header
and skips to credits if present.

SLICE 2: This module exists but run_metered_tool is NOT yet called from
dispatch (that is slice 3). All functions are implemented and unit-tested.

Honesty invariants (must hold across all code paths):
- preview_cost == actual charge (price_cents == what is committed)
- Only charge on real success (_receipt_is_error guard)
- Never charge twice (idempotent hold_id)
- Never silently swallow a Supabase error on the spend path (RAISES)
- Never go negative (enforced by the credit_reserve RPC check + DB constraint)
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Awaitable, Callable, Optional

from billing.pricing import (
    credit_cents,
    max_credits,
    is_paid as is_credit_paid_tool,
)
from billing.x402_gate import _receipt_is_error, _FAILURE_STATUSES  # noqa: F401 -- re-exported
from storage.supabase_client import rpc, select_rows

log = logging.getLogger("smb_broker.credits")

# Top-up URL shown in insufficient_credits messages
_TOPUP_URL = "https://hatchloop.dev/portal#topup"


# ---------------------------------------------------------------------------
# Public aliases for slice-3 call-site (mcp_server.py)
# ---------------------------------------------------------------------------

def is_credit_paid_tool(op: str) -> bool:  # noqa: F811 -- redefine for explicit re-export
    """True if this operation costs credits (price > 0)."""
    from billing.pricing import is_paid
    return is_paid(op)


def credit_cents_for(op: str) -> int:
    """Return the base price in credits (cents) for an operation."""
    return credit_cents(op)


# ---------------------------------------------------------------------------
# Identity helpers
# ---------------------------------------------------------------------------

def resolve_account(headers: dict[str, str]) -> Optional[str]:
    """Extract the account_id from the X-Agent-Identity JWT in headers.

    Returns the agent_id claim from a valid token, or None if the header is
    absent, the token is invalid, or the token is expired.
    The account_id is the agent_id embedded in the JWT (e.g. 'sub_CUSTOMER_ID'
    for subscription tokens issued by identity.issue_subscription_token).
    """
    token = None
    for key, val in headers.items():
        if key.lower() == "x-agent-identity":
            token = val.strip()
            break
    if not token:
        return None
    try:
        from agent_interface.identity import validate_token
        result = validate_token(token)
        if result.valid and result.identity:
            return result.identity.agent_id
    except Exception as exc:  # noqa: BLE001
        log.debug("resolve_account token validation failed: %s", exc)
    return None


def is_free_key(account_id: Optional[str]) -> bool:
    """True if this account_id corresponds to a free-tier key that bypasses
    credit deduction (falls through to the 50/day rate-limit path instead).

    Free keys are identified by the 'free_' prefix in their account_id.
    This covers developer trial keys issued before the credits system launched.
    """
    if not account_id:
        return False
    return str(account_id).startswith("free_")


# ---------------------------------------------------------------------------
# Credit RPC wrappers (all RAISE on Supabase failure -- fail closed)
# ---------------------------------------------------------------------------

async def reserve(account_id: str, amount: int, hold_id: str,
                  operation: Optional[str] = None,
                  operation_id: Optional[str] = None) -> dict[str, Any]:
    """Reserve `amount` credits for `account_id`. Returns the RPC response dict.

    Response shape: {ok: bool, reason_code?: str, balance_after?: int}
    RAISES RuntimeError if Supabase is unreachable (fail closed on spend path).
    """
    return await rpc("credit_reserve", {
        "p_account": account_id,
        "p_amount":  amount,
        "p_hold_id": hold_id,
        "p_op":      operation,
        "p_op_id":   operation_id,
    })


async def commit(hold_id: str, actual: int) -> dict[str, Any]:
    """Commit a hold, settling the actual cost. Refunds difference if actual < held.

    RAISES RuntimeError if Supabase is unreachable.
    """
    return await rpc("credit_commit", {
        "p_hold_id": hold_id,
        "p_actual":  actual,
    })


async def release(hold_id: str, reason: str = "release") -> dict[str, Any]:
    """Release a hold (tool failure path). Refunds the full held amount.

    RAISES RuntimeError if Supabase is unreachable.
    """
    return await rpc("credit_release", {
        "p_hold_id": hold_id,
        "p_reason":  reason,
    })


async def grant(account_id: str, amount: int, source: str = "grant",
                idempotency_key: Optional[str] = None,
                order_id: Optional[str] = None) -> dict[str, Any]:
    """Grant `amount` credits to `account_id`. Idempotent per idempotency_key.

    RAISES RuntimeError if Supabase is unreachable.
    """
    return await rpc("credit_grant", {
        "p_account":         account_id,
        "p_amount":          amount,
        "p_source":          source,
        "p_idempotency_key": idempotency_key,
        "p_order_id":        order_id,
    })


async def ensure_grandfather(account_id: str) -> bool:
    """Ensure a paid credit account exists; grant GRANDFATHER_CREDITS on first encounter.

    Called from the dispatch path BEFORE reserve so a new paid-key holder never
    hits an empty account. Idempotent: the credit_grant RPC uses ON CONFLICT DO
    NOTHING on the idempotency_key, so the grant fires exactly once even if this
    runs multiple times concurrently.

    Always fail-open (never raises): a missing courtesy grant must not block a
    legitimate paid call. Charges are still fail-closed via reserve().

    Returns True if a grandfather grant was applied, False otherwise.
    """
    import os as _os
    try:
        existing = await get_balance(account_id)
        if existing is not None:
            # Account already exists with a row -- no grant needed
            return False
        # First time we see this paid account -- apply one-time courtesy grant
        grandfather_credits = int(_os.getenv("GRANDFATHER_CREDITS", "1000"))
        if grandfather_credits <= 0:
            return False
        await grant(
            account_id=account_id,
            amount=grandfather_credits,
            source="grandfather",
            idempotency_key=f"grandfather_{account_id}",
        )
        log.info(
            "grandfather_grant_applied account=%s amount=%d",
            account_id, grandfather_credits,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("ensure_grandfather failed account=%s err=%s", account_id, exc)
        return False


async def _maybe_low_balance_nudge(account_id: str, balance: int) -> None:
    """Fire a low-balance email nudge when balance < 500cr and not recently notified.

    Dedup: reads low_balance_notified_at from credit_accounts. If the field is
    None or older than 24h, sends the nudge and updates the timestamp. Always
    fails silently (best-effort, never raises). Called from run_metered_tool
    after a successful commit.
    """
    _LOW_BALANCE = 500
    _NOTIFY_INTERVAL_S = 86400  # 24 hours

    if balance >= _LOW_BALANCE:
        return

    try:
        import os as _os
        import httpx as _httpx
        from datetime import datetime, timezone

        url = _os.getenv("SUPABASE_URL", "").rstrip("/")
        key = _os.getenv("SUPABASE_SERVICE_KEY", "") or _os.getenv("SUPABASE_ANON_KEY", "")
        if not url or not key:
            return

        # Read the account row for email + low_balance_notified_at.
        from storage.supabase_client import select_rows
        rows = await select_rows("credit_accounts", filters={"account_id": account_id}, limit=1)
        if not rows:
            return
        row = rows[0]
        email = row.get("email") or ""
        if not email:
            return

        # Dedup check: skip if notified within 24h.
        notified_at_str = row.get("low_balance_notified_at")
        if notified_at_str:
            try:
                notified_ts = datetime.fromisoformat(notified_at_str.replace("Z", "+00:00")).timestamp()
                if (datetime.now(timezone.utc).timestamp() - notified_ts) < _NOTIFY_INTERVAL_S:
                    return  # notified recently, skip
            except Exception:
                pass

        # Send the nudge.
        from billing.emails import send_low_balance_email
        await send_low_balance_email(email=email, balance=balance)

        # Update low_balance_notified_at (PATCH, best-effort).
        now_iso = datetime.now(timezone.utc).isoformat()
        async with _httpx.AsyncClient(timeout=6.0) as client:
            await client.patch(
                f"{url}/rest/v1/credit_accounts",
                headers={
                    "apikey": key,
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                    "Prefer": "return=minimal",
                },
                params={"account_id": f"eq.{account_id}"},
                json={"low_balance_notified_at": now_iso},
            )
    except Exception as exc:  # noqa: BLE001
        log.debug("_maybe_low_balance_nudge failed account=%s err=%s", account_id, exc)


async def get_balance(account_id: str) -> Optional[int]:
    """Return the current balance in credits for `account_id`, or None if not found.

    Uses a direct SELECT (not an RPC) -- read-only, fail-open (returns None on error).
    """
    try:
        rows = await select_rows("credit_accounts", filters={"account_id": account_id}, limit=1)
        if rows:
            return int(rows[0].get("balance_credits", 0))
    except Exception as exc:  # noqa: BLE001
        log.warning("get_balance failed account=%s err=%s", account_id, exc)
    return None


# ---------------------------------------------------------------------------
# run_metered_tool -- mirrors billing/x402_gate.run_paid_tool exactly
# ---------------------------------------------------------------------------

async def run_metered_tool(
    name: str,
    account_id: str,
    dispatch: Callable[[], Awaitable[dict]],
) -> dict:
    """Run a paid tool against the credits rail.

    Flow:
      1. reserve(MAX) -> if insufficient return honest failure WITHOUT dispatch
      2. dispatch()
      3. if _receipt_is_error(receipt): release(hold)  [no charge]
         else:                          commit(hold, actual_cents)
      4. attach credits{charged, balance} to receipt

    This function exists in slice 2 but is NOT yet called from dispatch
    (that wiring is slice 3 -- no behavior change until explicitly connected).

    RAISES RuntimeError only if Supabase is unreachable on the reserve call
    (fail closed). Post-dispatch errors (commit/release failures) are logged
    but do not propagate to avoid leaving the caller in a bad state.
    """
    max_cr = max_credits(name)
    hold_id = f"hold_{uuid.uuid4().hex}"
    operation_id = uuid.uuid4().hex

    # --- Step 1: Reserve MAX ---
    try:
        reserve_result = await reserve(
            account_id=account_id,
            amount=max_cr,
            hold_id=hold_id,
            operation=name,
            operation_id=operation_id,
        )
    except RuntimeError:
        # Supabase unreachable -- fail closed: refuse paid work
        raise

    if not reserve_result.get("ok"):
        reason = reserve_result.get("reason_code", "insufficient_credits")
        balance = reserve_result.get("balance", reserve_result.get("balance_after", 0))
        # Honest failure: do NOT dispatch, cost = 0
        return {
            "status":       "failure",
            "reason_code":  "insufficient_credits",
            "human_message": (
                f"Insufficient credits to run {name!r}. "
                f"Required: {max_cr} credits, balance: {balance} credits. "
                f"Top up at {_TOPUP_URL}"
            ),
            "cost": {"amount": 0.0, "currency": "USD", "basis": "per_call"},
            "credits": {"charged": 0, "balance": balance},
        }

    # --- Step 2: Dispatch ---
    try:
        receipt = await dispatch()
    except Exception as exc:
        # Dispatch raised -- release the hold, re-raise
        try:
            await release(hold_id, reason="dispatch_exception")
        except Exception as rel_exc:
            log.error(
                "credits release failed after dispatch exception "
                "hold=%s err=%s orig=%s", hold_id, rel_exc, exc
            )
        raise

    # --- Step 3: Commit or release based on success ---
    is_error = _receipt_is_error(receipt)

    if is_error:
        # Tool failed -- release hold, no charge
        try:
            rel_result = await release(hold_id, reason="tool_failure")
            # Same rule as the commit path: a missing key is UNKNOWN, not zero.
            balance_after = rel_result.get("balance_after")
        except Exception as exc:
            log.error("credits release failed hold=%s err=%s", hold_id, exc)
            balance_after = None
        actual_charged = 0
    else:
        # Tool succeeded -- commit actual cost
        # Derive actual cents from receipt.cost.amount (USD) if present,
        # else fall back to the fixed price_cents (correct for fixed-price ops).
        cost_record = receipt.get("cost") if isinstance(receipt, dict) else None
        if isinstance(cost_record, dict) and cost_record.get("amount") is not None:
            actual_cents = round(float(cost_record["amount"]) * 100)
        else:
            actual_cents = credit_cents(name)
        # Clamp to [0, max_cr] - never commit more than was held.
        #
        # THE CLAMP IS ALSO A SUBSIDY. When the real cost exceeds the reserve we
        # charge the reserve and absorb the difference, silently. That is the
        # right customer behaviour - we quoted a ceiling and must honour it -
        # but it was invisible, so an unbounded loss could accumulate one
        # reasonable-looking call at a time. A UAE SMS costs ~$0.109 against a
        # 22-credit ceiling; the gap is ours.
        #
        # Surfaced by an independent DeepSeek review (2026-08-26) and confirmed
        # in the code: the finding was real even though several of that review's
        # other findings were not.
        uncapped_cents = actual_cents
        actual_cents = max(0, min(actual_cents, max_cr))
        if uncapped_cents > max_cr:
            try:
                from billing import subsidy as _subsidy
                await _subsidy.record(
                    tool=name,
                    our_cost_usd=uncapped_cents / 100,
                    charged_usd=actual_cents / 100,
                    agent_id=account_id,
                )
            except Exception:  # noqa: BLE001 - measurement must not break billing
                pass

        try:
            commit_result = await commit(hold_id, actual_cents)
            # None, not 0. A missing key means the ledger did not tell us the
            # balance - which is not the same as the balance being zero.
            balance_after = commit_result.get("balance_after")
        except Exception as exc:
            log.error("credits commit failed hold=%s err=%s", hold_id, exc)
            # Could not confirm -- attempt release to avoid permanent hold
            try:
                await release(hold_id, reason="commit_failed")
            except Exception:
                pass
            # WAS `balance_after = 0`, AND THAT NUMBER REACHED THE CUSTOMER
            # TWICE. It went onto the receipt, so someone holding 50,000
            # credits was told their balance was zero; and it fell through the
            # low-balance test below, which emailed them a warning about
            # running out - on the strength of a number we invented because a
            # write failed.
            #
            # Unknown is a value. It is the honest one here.
            balance_after = None
        actual_charged = actual_cents

    # --- Step 4: Attach credits info to receipt ---
    if isinstance(receipt, dict):
        receipt = dict(receipt)
        receipt["credits"] = {
            "charged": actual_charged,
            "balance": balance_after,
        }
        if balance_after is None:
            receipt["credits"]["balance_note"] = (
                "Your balance could not be confirmed on this call - the charge "
                "was applied but the ledger did not return a balance. This is "
                "NOT a balance of zero. Check the portal for the real figure.")

    # --- Step 5: Fire low-balance nudge (slice 6) ---
    # Non-blocking; never raises. Dedup enforced by low_balance_notified_at (24h).
    # `balance_after is not None` FIRST. This test used to run against a
    # fabricated zero and email the customer a low-balance warning whenever a
    # commit failed - telling someone with a full account that they were
    # nearly out.
    if not is_error and balance_after is not None and balance_after < 500:
        try:
            import asyncio as _asyncio
            _asyncio.create_task(_maybe_low_balance_nudge(account_id, balance_after))
        except Exception:  # noqa: BLE001
            pass

    return receipt
