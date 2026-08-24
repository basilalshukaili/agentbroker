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
            balance_after = rel_result.get("balance_after", 0)
        except Exception as exc:
            log.error("credits release failed hold=%s err=%s", hold_id, exc)
            balance_after = 0
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
        # Clamp to [0, max_cr] for safety (never commit more than was held)
        actual_cents = max(0, min(actual_cents, max_cr))

        try:
            commit_result = await commit(hold_id, actual_cents)
            balance_after = commit_result.get("balance_after", 0)
        except Exception as exc:
            log.error("credits commit failed hold=%s err=%s", hold_id, exc)
            # Could not confirm -- attempt release to avoid permanent hold
            try:
                await release(hold_id, reason="commit_failed")
            except Exception:
                pass
            balance_after = 0
        actual_charged = actual_cents

    # --- Step 4: Attach credits info to receipt ---
    if isinstance(receipt, dict):
        receipt = dict(receipt)
        receipt["credits"] = {
            "charged": actual_charged,
            "balance": balance_after,
        }
    return receipt
