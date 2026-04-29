# ADR-0001: Use Python + FastAPI as the primary web framework

**Status:** Accepted  
**Date:** 2026-04-27  
**Agents:** ArchitectAgent, OrchestratorAgent

## Context
We need a web framework that supports async I/O (channel adapters are network-bound), native JSON schema validation (all 12 operations are schema-validated), and automatic OpenAPI generation (discovery endpoint requirement in §4.4).

## Decision
Use Python 3.11+ with FastAPI and Pydantic v2.

## Consequences
- **Positive:** OpenAPI spec generated automatically from route definitions; Pydantic v2 models serve as both request validators and schema documentation; async handlers map cleanly to async channel adapter calls.
- **Negative:** Python GIL limits CPU-bound work; mitigated because all hot paths are I/O-bound (DB, channel APIs, Redis).
- **Constraint:** All Pydantic models are the single source of truth for schemas. `/api/schemas/*.json` files are exported from these models, not hand-authored.
