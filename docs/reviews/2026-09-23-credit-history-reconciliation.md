# Historical credit commit reconciliation — 2026-09-23

The HatchLoop Supabase target has `public.credit_accounts` and
`public.credit_ledger` but no `credit_ledger.actual_credits` column. The
read-only Management API preflight counted 84 ledger rows: 30 holds, 26 commits
and 25 refunds. All 26 existing commit markers will have unknown (`NULL`)
actual cost when the reviewed billing migration adds that column, so draft
PR 11 deliberately refuses their exact replay. This note and migration provide
a guarded candidate to recover that metadata without changing balances.

An aggregate-only target query classified the history as 5 full commits, 21
partial commits with a bounded same-account refund, 4 released holds, 0 open
holds, 0 inconsistent or orphan commit/refund rows, and 0 accounts whose
`lifetime_spent` disagrees with the derived actual spend. No account IDs,
orders, customer data or secrets were printed. These checks establish a
possible derivation, not a completed real purchase or permission to mutate
the target.

`migrations/credits_history_reconcile.sql` is a **separate post-migration
step**. It locks accounts then ledger, rejects missing/duplicate hold keys,
unmatched accounts, invalid commit/refund amounts and reasons, conflicting
already-filled actuals, and lifetime-spend mismatches. If every invariant
passes, it fills only NULL commit `actual_credits` values with held amount
minus the partial refund. The transaction rolls back as a unit on any
ambiguity and is idempotent on a clean rerun. It does not alter balances,
lifetime totals, function bodies or privileges.

The guarded `tests/integration/check_credit_history_sql.py` passed twice on
the disposable PostgreSQL 18.4 cluster at `127.0.0.1:55483`, database
`tm_credit_test_history_20260923`. It proved rollback on an over-refund,
exact full/partial derivation, successful commit replay after reconciliation,
rejection of a mismatched replay and release-after-commit, rerun idempotency,
rejection of a conflicting non-NULL actual, rejection of a lifetime-spend
mismatch and preservation of the four-RPC caller boundary. The cluster was
stopped afterward. Running the test without its explicit disposable guard
refuses before any database access.

Fable owns independent SQL review and the release plan on board `edb7902e`.
Before a target write: review PRs 3/10/11 together, take an exact target
snapshot and define rollback, apply the reviewed billing migration, verify
the service-role-only RPC grants, rerun the aggregate preflight, then run this
reconciliation in a bounded window and read back all invariants. Billing must
remain disabled until authorized purchase-to-entitlement-to-first-paid-call
acceptance also passes. This branch does not apply a target migration,
backfill, billing switch or production-chat test.
