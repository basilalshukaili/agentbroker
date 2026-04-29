# ADR-0002: Compliance as a mandatory pre-check gate, not middleware

**Status:** Accepted  
**Date:** 2026-04-27  
**Agents:** ArchitectAgent, ComplianceAgent, OrchestratorAgent

## Context
Regulations (TCPA, 10DLC, GDPR, CASL, CAN-SPAM) are not optional. A service that routes communications at scale and ignores them will be shut down before WinRate becomes relevant. The question is whether compliance logic lives as middleware, as a separate service, or as an explicit function call in each channel adapter.

## Decision
Compliance is an **explicit function call** (`compliance.pre_check(...)`) that every channel adapter MUST call before dispatching outbound communications. It is not middleware (which can be accidentally bypassed). It is not a separate network service (which adds latency and a failure mode). It is a synchronous function call that returns `allow` or raises `ComplianceViolationError`.

## Consequences
- **Positive:** Cannot be bypassed by routing around middleware; every channel adapter author sees the call site; static analysis can enforce it.
- **Positive:** `ComplianceViolationError` is a first-class error type surfaced in the API error taxonomy.
- **Negative:** Channel adapters must import and call compliance explicitly — enforced by code review + contract test.
- **Constraint:** ComplianceAgent is the only agent permitted to authorize an outbound communication. Other agents request authorization; they cannot bypass it (§2.13).
