"""
P14 Gate Check — verifies all release gates before ship.

Run:  python check_gates.py
Exit: 0 = all gates pass, 1 = one or more gates failed.
"""
import asyncio
import sys
import traceback

PASS = "[PASS]"
FAIL = "[FAIL]"
WARN = "[WARN]"

results: list[tuple[str, bool, str]] = []


def gate(name: str, passed: bool, note: str = "") -> None:
    results.append((name, passed, note))
    status = PASS if passed else FAIL
    print(f"  {status} {name}" + (f" — {note}" if note else ""))


# ---------------------------------------------------------------------------
# Gate 1: All 12 operations importable and callable
# ---------------------------------------------------------------------------
print("\n[Gate 1] Core operations")
try:
    from core.find_business import handle_find_business
    from core.verify_business import handle_verify_business
    from core.send_message import handle_send_message
    from core.capture_lead import handle_capture_lead
    from core.schedule_appointment import handle_schedule_appointment
    from core.send_transactional_confirmation import handle_send_transactional_confirmation
    from core.handle_inbound import handle_inbound
    from core.escalate_to_human import handle_escalate_to_human
    from core.status_outcome import handle_get_status, handle_get_outcome
    from core.preview_cost import handle_preview_cost
    gate("All 12 operations importable", True)
except Exception as e:
    gate("All 12 operations importable", False, str(e))


# Gate 1b: find_business smoke test
try:
    from core.models import FindBusinessRequest, LocationFilter, Vertical, OperationStatus
    req = FindBusinessRequest(
        vertical=Vertical.PERSONAL_SERVICES,
        location=LocationFilter(zip_or_city="30309"),
        capability="haircut",
    )
    receipt = asyncio.get_event_loop().run_until_complete(handle_find_business(req))
    gate("find_business returns SUCCESS", receipt.status == OperationStatus.SUCCESS)
except Exception as e:
    gate("find_business returns SUCCESS", False, str(e))


# ---------------------------------------------------------------------------
# Gate 2: Manifest structure
# ---------------------------------------------------------------------------
print("\n[Gate 2] Manifest")
try:
    import json
    from pathlib import Path
    manifest = json.loads((Path("manifest/manifest.json")).read_text())
    ops = manifest.get("operations", [])
    gate("Manifest loads", True)
    gate("At least 12 operations in manifest", len(ops) >= 12, f"found {len(ops)}")

    expected_ops = {
        "find_business", "verify_business", "send_message", "capture_lead",
        "schedule_appointment", "send_transactional_confirmation", "handle_inbound",
        "escalate_to_human", "get_status", "get_outcome", "preview_cost", "self_test",
    }
    actual_ops = {op["name"] for op in ops}
    missing = expected_ops - actual_ops
    gate("All expected operations present", len(missing) == 0,
         f"missing: {missing}" if missing else "")

    all_have_examples = all(len(op.get("examples", [])) >= 1 for op in ops)
    gate("Every operation has at least 1 example", all_have_examples)
except Exception as e:
    gate("Manifest loads", False, str(e))


# ---------------------------------------------------------------------------
# Gate 3: Compliance
# ---------------------------------------------------------------------------
print("\n[Gate 3] Compliance pre_check gate")
try:
    from compliance.pre_check import pre_check
    from core.models import ComplianceViolationError

    # 3a: Gambling blocked
    try:
        pre_check(
            recipient_id="+14045550000", channel="sms", message_type="marketing",
            content="Win big at the casino!", country_code="US",
        )
        gate("Gambling content blocked", False, "pre_check did not raise")
    except ComplianceViolationError:
        gate("Gambling content blocked", True)

    # 3b: Opted-out recipient blocked
    from compliance.consent_store import ConsentStore, ConsentStatus
    from compliance import consent_store as cs_module
    store = ConsentStore()
    store.record_consent("+15550000001", "sms", "marketing", ConsentStatus.OPTED_IN,
                         "US", "express_written", "gate_check")
    store.revoke_consent("+15550000001", "sms", "marketing", "STOP_keyword")
    original = cs_module._store
    cs_module._store = store
    try:
        pre_check(
            recipient_id="+15550000001", channel="sms", message_type="marketing",
            content="Hello!", country_code="US",
        )
        gate("Opted-out recipient blocked", False, "pre_check did not raise")
    except ComplianceViolationError:
        gate("Opted-out recipient blocked", True)
    finally:
        cs_module._store = original

    # 3c: Transactional EU allowed
    try:
        pre_check(
            recipient_id="user@example.com", channel="email", message_type="transactional",
            content="Your booking is confirmed.", country_code="DE",
        )
        gate("Transactional EU email allowed", True)
    except ComplianceViolationError as e:
        gate("Transactional EU email allowed", False, str(e))

except Exception as e:
    gate("Compliance gate importable", False, str(e))


# ---------------------------------------------------------------------------
# Gate 4: Audit log PII hashing
# ---------------------------------------------------------------------------
print("\n[Gate 4] Audit log PII")
try:
    from compliance.audit_log import AuditLog, AuditEventType
    log = AuditLog()
    record = log.record(AuditEventType.OUTBOUND_DISPATCHED, recipient_id="+14045550300")
    has_hash = record.recipient_id_hash is not None
    no_plaintext = "+14045550300" not in str(record.model_dump())
    gate("Audit log stores hash, not plaintext", has_hash and no_plaintext)
except Exception as e:
    gate("Audit log PII hashing", False, str(e))


# ---------------------------------------------------------------------------
# Gate 5: Supply directory — 20 seed SMBs
# ---------------------------------------------------------------------------
print("\n[Gate 5] Supply directory")
try:
    from supply.smb_directory import SMBDirectory
    d = SMBDirectory()
    size = d.size()
    gate("20 seed SMBs in directory", size >= 20, f"found {size}")

    # Verticals represented
    from supply.smb_directory import _DIRECTORY
    verticals = {smb.vertical.value for smb in _DIRECTORY.values()}
    gate("All 3 wedge verticals seeded", len(verticals) == 3, str(verticals))
except Exception as e:
    gate("Supply directory", False, str(e))


# ---------------------------------------------------------------------------
# Gate 6: Circuit breaker
# ---------------------------------------------------------------------------
print("\n[Gate 6] Reliability")
try:
    from reliability.circuit_breaker import CircuitBreaker, CircuitState
    cb = CircuitBreaker(name="gate_check_cb", failure_threshold=3)
    for _ in range(3):
        cb.record_failure()
    gate("Circuit breaker opens after threshold", cb.state == CircuitState.OPEN)
except Exception as e:
    gate("Circuit breaker", False, str(e))


# ---------------------------------------------------------------------------
# Gate 7: Idempotency store
# ---------------------------------------------------------------------------
print("\n[Gate 7] Idempotency store")
try:
    from storage.idempotency_store import IdempotencyStore
    store = IdempotencyStore()
    store.set("gate_agent", "find_business", "key_001", {"ok": True})
    exists = store.exists("gate_agent", "find_business", "key_001")
    cached = store.get("gate_agent", "find_business", "key_001")
    gate("Idempotency store set/get round-trip", exists and cached == {"ok": True})

    # Different agents don't collide
    store.set("agent_A", "op", "key", {"x": 1})
    store.set("agent_B", "op", "key", {"x": 2})
    no_collision = (
        store.get("agent_A", "op", "key")["x"] == 1 and
        store.get("agent_B", "op", "key")["x"] == 2
    )
    gate("Agent-scoped keys don't collide", no_collision)
except Exception as e:
    gate("Idempotency store", False, str(e))


# ---------------------------------------------------------------------------
# Gate 8: Self-test
# ---------------------------------------------------------------------------
print("\n[Gate 8] Self-test")
try:
    from agent_interface.self_test import run_self_test
    report = asyncio.get_event_loop().run_until_complete(run_self_test())
    gate(f"Self-test all_passed ({report.passed_checks}/{report.total_checks})",
         report.all_passed,
         " | ".join(f"{c.name}:{c.error}" for c in report.checks if not c.passed))
except Exception as e:
    gate("Self-test", False, str(e))


# ---------------------------------------------------------------------------
# Gate 9: Preview cost covers all 12 ops
# ---------------------------------------------------------------------------
print("\n[Gate 9] Preview cost")
try:
    from core.preview_cost import handle_preview_cost
    from core.models import PreviewCostRequest
    ops = [
        "find_business", "verify_business", "send_message", "capture_lead",
        "schedule_appointment", "send_transactional_confirmation", "handle_inbound",
        "escalate_to_human", "get_status", "get_outcome", "preview_cost", "self_test",
    ]
    all_ok = True
    for op in ops:
        resp = asyncio.get_event_loop().run_until_complete(
            handle_preview_cost(PreviewCostRequest(operation=op, params={}))
        )
        if resp.estimated_cost_usd < 0:
            all_ok = False
    gate("All 12 ops have valid preview_cost", all_ok)
except Exception as e:
    gate("Preview cost", False, str(e))


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print("\n" + "="*60)
total = len(results)
passed = sum(1 for _, p, _ in results if p)
failed = total - passed

if failed == 0:
    print(f"[PASS] ALL {total} GATES PASSED -- v0.1.0 READY FOR RELEASE")
else:
    print(f"[FAIL] {failed}/{total} gates FAILED")
    for name, ok, note in results:
        if not ok:
            print(f"   FAILED: {name}" + (f" — {note}" if note else ""))

sys.exit(0 if failed == 0 else 1)
