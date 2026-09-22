# Credit-grant replay binding — 2026-09-23

The original `credit_grant` incremented the requested account before inserting a
ledger row under the order idempotency key. A duplicate key rolled the increment
back and returned `ok: true, idempotent: true` without checking which account,
amount, source or order won originally. A changed Polar customer or package on a
redelivery could therefore be acknowledged without granting that entitlement.
The same SQL accepted nonpositive grant amounts. These are source-level findings;
they do not assert a live exploit or a completed payment.

`migrations/credits_billing.sql` now rejects empty accounts and nonpositive
amounts. On a duplicate key, it locks and compares the existing ledger tuple
before acknowledging exact replay. A mismatch raises SQLSTATE 23505, so the
upsert rolls back and the webhook remains retryable. The function uses a fixed
empty search path with schema-qualified tables. All four billing RPCs now
revoke inherited PUBLIC/anon/authenticated execution and grant execution to
`service_role`. Existing
installations must reapply the reviewed migration; a source change alone does
not alter the target database.

The guarded `tests/integration/check_credit_grant_sql.py` requires a disposable
loopback `tm_credit_test_*` database on a non-default port and an explicit
`TM_CREDIT_SQL_TEST_DISPOSABLE=yes` flag. It reapplies the migration after
deliberately granting EXECUTE to PUBLIC for all four RPCs, verifies the revokes
and actual denied-role calls, confirms all four service-role paths, tests exact and mismatched
replays, and races two deliveries while the first transaction is uncommitted.
On 2026-09-23, it passed twice against a separate PostgreSQL 18.4 WSL cluster
at `127.0.0.1:55483`; the pre-existing port-5432 listener and target Supabase
account were untouched. The database has only synthetic rows.

This is one release slice. The sibling billing RPCs still need input, replay
binding, owner-authority and terminal-state review. In the current source,
`credit_reserve` accepts a negative amount, `credit_commit` accepts a negative
actual amount, and `credit_release` does not refuse a hold already committed.
Those are separate money-moving defects and keep billing disabled even after
the execution grants are restricted. The target database's
applied schema, role grants, historical paid-but-ungranted rows, concurrent
marker behavior, provider timeout and durable key/email delivery remain
unverified. No billing enablement, real checkout, merge or deployment follows
from this isolated test. Claude/Fable owns independent integration review and
the authorized end-to-end release acceptance tracked on board `edb7902e`.
