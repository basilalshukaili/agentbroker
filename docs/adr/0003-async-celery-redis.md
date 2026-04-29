# ADR-0003: Use Celery + Redis for async operations

**Status:** Accepted  
**Date:** 2026-04-27  
**Agents:** ArchitectAgent, ReliabilityAgent

## Context
Operations like `schedule_appointment` (voice-AI channel), `handle_inbound`, and `escalate_to_human` are async-by-default — the agent gets a `pending_async` response immediately and the real outcome arrives minutes later via webhook. We need a durable job queue with retry support, ETA scheduling, and visibility into job state.

## Decision
Use **Celery** with **Redis** as the broker and result backend. Web workers return `pending_async` and enqueue a Celery task. A separate Celery worker pool executes the task, writes the terminal `OutcomeReceipt` to the outcome store, and fires the signed webhook.

## Consequences
- **Positive:** Proven at scale; supports exponential backoff retries; task state queryable via Redis.
- **Positive:** Web process and worker process are independently scalable.
- **Negative:** Adds operational complexity (two process types); mitigated by docker-compose multi-service definition.
- **Constraint:** Every async job must have a terminal state (success | failure | partial). No fire-and-forget (§9.17). The job ID maps 1:1 to the `operation_id` in OutcomeReceipt.
