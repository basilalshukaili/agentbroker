# ADR-0004: Idempotency store backed by Redis with PostgreSQL fallback

**Status:** Accepted  
**Date:** 2026-04-27  
**Agents:** ArchitectAgent, ReliabilityAgent

## Context
Every state-changing operation requires an `Idempotency-Key` header (§9.5). Duplicate requests with the same key must return the cached OutcomeReceipt without re-executing the operation. The store must be fast (sub-ms check at request start) and durable (a key must survive worker restart).

## Decision
Idempotency keys are stored in **Redis** with a TTL of 24 hours. On write, the key is also written to **PostgreSQL** as a durable backup. On lookup, Redis is checked first; on Redis miss, PostgreSQL is checked.

## Consequences
- **Positive:** Redis provides sub-ms lookup for the common case.
- **Positive:** PostgreSQL backup prevents losing idempotency guarantees during Redis restarts.
- **Negative:** Dual-write complexity; acceptable because the write path is async and non-blocking.
- **Constraint:** Idempotency-Key uniqueness scope is per (agent_id, operation, key). Same key from different agents does NOT collide.
