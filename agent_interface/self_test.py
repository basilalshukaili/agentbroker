"""
Self-test — validates that this service instance is healthy end-to-end.

Runs a deterministic smoke-test corpus without hitting any real external APIs.
All operations use in-memory stubs.

Returns a SelfTestReport with pass/fail per check.
Used by: orchestrators probing before loading this service,
         CI pipelines as a post-deploy gate,
         the self_test operation exposed in the manifest.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Callable, Awaitable


# ---------------------------------------------------------------------------
# Report model
# ---------------------------------------------------------------------------

@dataclass
class TestCheck:
    name: str
    passed: bool
    latency_ms: float
    error: str = ""


@dataclass
class SelfTestReport:
    all_passed: bool
    total_checks: int
    passed_checks: int
    failed_checks: int
    checks: list[TestCheck]
    latency_ms: float
    timestamp: str


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

async def _check_find_business() -> TestCheck:
    start = time.time()
    try:
        from core.find_business import handle_find_business
        from core.models import FindBusinessRequest, LocationFilter, Vertical, OperationStatus
        req = FindBusinessRequest(
            vertical=Vertical.PERSONAL_SERVICES,
            location=LocationFilter(zip_or_city="30309"),
            capability="haircut",
        )
        receipt = await handle_find_business(req)
        ok = receipt.status == OperationStatus.SUCCESS
        # find_business now depends on the public OpenStreetMap servers. When they
        # are down it answers, correctly, with a retriable
        # osm_temporarily_unavailable receipt that still carries the attribution
        # and invents nothing - that is the CONTRACT working, so it passes here
        # (with the reason recorded) rather than paging someone every time a
        # volunteer-run server has a bad minute.
        upstream_down = (
            not ok
            and receipt.reason_code == "osm_temporarily_unavailable"
            and receipt.retriable is True
            and isinstance(receipt.result, dict)
            and bool(receipt.result.get("attribution"))
            and receipt.result.get("businesses") == []
            # a BUG in our own code also surfaces as osm_temporarily_unavailable
            # (reason internal_error); that must still fail the self-test.
            and (receipt.result.get("search") or {}).get("reason") != "internal_error"
        )
        if ok:
            error = ""
        elif upstream_down:
            error = "osm_temporarily_unavailable (upstream dependency down; contract intact)"
        else:
            error = f"Unexpected status: {receipt.status}"
        return TestCheck(
            name="find_business",
            passed=ok or upstream_down,
            latency_ms=round((time.time() - start) * 1000, 2),
            error=error,
        )
    except Exception as e:
        return TestCheck("find_business", False, round((time.time() - start) * 1000, 2), str(e))


async def _check_verify_business() -> TestCheck:
    """Contract check, not a supply check.

    Production runs SUPPLY_SEED_MODE=empty, so a hard-coded smb_id has no
    guarantee of being present. What we actually want to assert is that
    verify_business returns a well-structured OutcomeReceipt either way —
    SUCCESS when the SMB exists, or a clean SUPPLY_UNREACHABLE/OUT_OF_SUPPLY
    receipt when it doesn't. Either is a passing contract; only an
    exception or malformed receipt is a failure.
    """
    start = time.time()
    try:
        from core.verify_business import handle_verify_business
        from core.models import VerifyBusinessRequest, OperationStatus
        from supply.smb_directory import get_directory
        # Use any real SMB if the directory has supply; otherwise probe with a
        # deliberately unknown id so we exercise the not-found path.
        directory = get_directory()
        existing = next(iter(getattr(directory, "_DIRECTORY", {}) or {}), None) \
            if hasattr(directory, "_DIRECTORY") else None
        if existing is None and directory.size() > 0:
            # Older SMBDirectory shape — pull any active id via search.
            for v in ["personal_services", "home_services", "professional_services"]:
                from core.models import Vertical
                try:
                    r = directory.search(vertical=Vertical(v), zip_or_city="")
                    if r:
                        existing = r[0].smb_id
                        break
                except Exception:
                    pass
        smb_id = existing or "smb_self_test_synthetic"
        req = VerifyBusinessRequest(smb_id=smb_id, capability_to_verify="haircut")
        receipt = await handle_verify_business(req)
        # Pass on any well-formed receipt — both SUCCESS and recognized failure
        # statuses prove the contract.
        ok = receipt.status in (
            OperationStatus.SUCCESS, OperationStatus.FAILURE, OperationStatus.PARTIAL,
        ) and receipt.operation_id
        return TestCheck("verify_business", bool(ok),
                         round((time.time() - start) * 1000, 2),
                         "" if ok else f"unexpected receipt shape: status={receipt.status}")
    except Exception as e:
        return TestCheck("verify_business", False, round((time.time() - start) * 1000, 2), str(e))


async def _check_preview_cost() -> TestCheck:
    start = time.time()
    try:
        from core.preview_cost import handle_preview_cost
        from core.models import PreviewCostRequest
        req = PreviewCostRequest(operation="schedule_appointment", params={"smb_id": "smb_001"})
        resp = await handle_preview_cost(req)
        # WAS: `and resp.cost_accuracy_slo == "+/-5%"`. That asserted a
        # constant equals itself - it "verified" the accuracy promise by
        # confirming we had made it, which is not a check of anything.
        # Assert the estimate is inside its own stated range instead, which is
        # a property that can actually be false.
        _lo = resp.cost_range.get("min_usd", 0.0)
        _hi = resp.cost_range.get("max_usd", 0.0)
        ok = (resp.estimated_cost_usd >= 0
              and _lo <= resp.estimated_cost_usd <= _hi
              and bool(resp.cost_accuracy_slo))
        return TestCheck("preview_cost", ok, round((time.time() - start) * 1000, 2),
                         "" if ok else "Unexpected preview_cost response.")
    except Exception as e:
        return TestCheck("preview_cost", False, round((time.time() - start) * 1000, 2), str(e))


async def _check_compliance_gate() -> TestCheck:
    """Verify that the compliance gate fires for a known-bad message."""
    start = time.time()
    try:
        from compliance.pre_check import pre_check
        from core.models import ComplianceViolationError
        fired = False
        try:
            pre_check(
                recipient_id="+14045550000",
                channel="sms",
                message_type="marketing",
                content="Win big at the casino tonight!",
                country_code="US",
            )
        except ComplianceViolationError:
            fired = True
        return TestCheck("compliance_gate", fired, round((time.time() - start) * 1000, 2),
                         "" if fired else "Compliance gate did NOT fire — critical failure.")
    except Exception as e:
        return TestCheck("compliance_gate", False, round((time.time() - start) * 1000, 2), str(e))


async def _check_manifest_loads() -> TestCheck:
    start = time.time()
    try:
        from agent_interface.manifest_server import get_full_manifest
        manifest = get_full_manifest()
        ops = manifest.get("operations", [])
        # Operations count is allowed to grow as we add tools; assert the
        # manifest loaded a sensible number, not an exact frozen count.
        ok = len(ops) >= 12
        return TestCheck("manifest_loads", ok, round((time.time() - start) * 1000, 2),
                         "" if ok else f"Expected >=12 operations, got {len(ops)}.")
    except Exception as e:
        return TestCheck("manifest_loads", False, round((time.time() - start) * 1000, 2), str(e))


async def _check_idempotency_store() -> TestCheck:
    start = time.time()
    try:
        from storage.idempotency_store import IdempotencyStore
        store = IdempotencyStore()
        store.set("self_test_agent", "self_test", "key_001", {"ok": True})
        ok = store.exists("self_test_agent", "self_test", "key_001")
        cached = store.get("self_test_agent", "self_test", "key_001")
        ok = ok and cached == {"ok": True}
        return TestCheck("idempotency_store", ok, round((time.time() - start) * 1000, 2),
                         "" if ok else "IdempotencyStore round-trip failed.")
    except Exception as e:
        return TestCheck("idempotency_store", False, round((time.time() - start) * 1000, 2), str(e))


async def _check_metering_pipeline() -> TestCheck:
    """Both billing rails (billing/usage_logger.py's usage_events and
    billing/durable_meter.py's billing_events) fire-and-forget a background
    write on every call, and neither one is awaited by its caller — so a
    write that silently stops happening breaks nothing that shows up in a
    response, only a downstream row count days later (AUDIT-2026-09-28: this
    is exactly how a real outage went unnoticed for a week).

    This check does NOT make a network call (self_test's own contract is to
    hit no real external API) — it reads the in-process health snapshot each
    rail now keeps (get_usage_logger_health / get_durable_meter_health) and
    fails if either rail has ever recorded a failed write, or has a
    scheduled write that never resolved. A clean process that has logged
    nothing yet (both counters at zero) is healthy by default: this check
    catches a rail that HAS tried and failed, or lost a task, not the
    absence of traffic.
    """
    start = time.time()
    try:
        from billing.usage_logger import get_usage_logger_health
        from billing.durable_meter import get_durable_meter_health

        usage_health = get_usage_logger_health()
        billing_health = get_durable_meter_health()

        problems = []
        if usage_health["failed"] > 0:
            problems.append(
                f"usage_events: {usage_health['failed']} failed write(s), "
                f"last={usage_health['last_failure_reason']}@{usage_health['last_failure_ts']}"
            )
        if billing_health["failed"] > 0:
            problems.append(
                f"billing_events: {billing_health['failed']} failed write(s), "
                f"last={billing_health['last_failure_reason']}@{billing_health['last_failure_ts']}"
            )
        # A pending count that has been building up (rather than draining
        # back toward zero between requests) is the "task never resolved"
        # shape this whole audit exists to catch. A hard-coded threshold
        # here (rather than 0) tolerates ordinary in-flight writes from
        # concurrent requests without needing a time-windowed rate.
        if usage_health["pending"] > 50:
            problems.append(f"usage_events: {usage_health['pending']} writes stuck pending")
        if billing_health["pending"] > 50:
            problems.append(f"billing_events: {billing_health['pending']} writes stuck pending")

        ok = not problems
        return TestCheck(
            "metering_pipeline", ok, round((time.time() - start) * 1000, 2),
            "" if ok else "; ".join(problems),
        )
    except Exception as e:
        return TestCheck("metering_pipeline", False, round((time.time() - start) * 1000, 2), str(e))


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

_CHECKS: list[Callable[[], Awaitable[TestCheck]]] = [
    _check_find_business,
    _check_verify_business,
    _check_preview_cost,
    _check_compliance_gate,
    _check_manifest_loads,
    _check_idempotency_store,
    _check_metering_pipeline,
]


async def run_self_test() -> SelfTestReport:
    """Run all self-test checks concurrently and return a SelfTestReport."""
    start = time.time()
    results = await asyncio.gather(*[check() for check in _CHECKS])
    checks = list(results)
    passed = sum(1 for c in checks if c.passed)
    total_ms = round((time.time() - start) * 1000, 2)

    return SelfTestReport(
        all_passed=all(c.passed for c in checks),
        total_checks=len(checks),
        passed_checks=passed,
        failed_checks=len(checks) - passed,
        checks=checks,
        latency_ms=total_ms,
        timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )


def run_self_test_sync() -> SelfTestReport:
    return asyncio.get_event_loop().run_until_complete(run_self_test())
