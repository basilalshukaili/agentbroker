# Durable Polar fulfillment

Payment intake and paid-tool activation are separate. Keep `CREDITS_ENABLED`,
`DATA_METERING_ENABLED` and the x402 switch off until the complete provider money
path has been verified. A received, signed paid order must still be fulfilled
durably when the scoped fulfillment service is configured.

Apply `migrations/spine/014_polar_fulfillment.sql` as `spine_owner` after creating
the `polar_fulfillment` role with NOLOGIN, NOINHERIT and no elevated privileges.
The PostgREST authenticator must be allowed to assume this role. Mint its scoped
JWT using the existing server-side signing system and install it only as
`POLAR_FULFILLMENT_KEY`. Never substitute a service-role credential or grant the
generic credit RPCs to anonymous callers. The role gets explicit execution
permission on claim, complete, release and refund only; receipt and credit tables
remain inaccessible directly.

An order permanently binds its customer, account, product, credit amount, email
digest, plan and issuance version. A 30-second lease plus a fencing generation
prevents concurrent or expired workers from completing another worker's claim.
Completion locks the customer/order and atomically grants credits, verifies the
OAuth account link and records completion. Replays reproduce the stored identity
and never issue a second grant. A mismatch in an existing account link or historical
grant rolls the transaction back. Plan upgrades require a separate policy; this
path does not overwrite a conflicting link.

Customer reactivation after a terminal refund also needs a separate policy.
A different new paid order for a revoked customer is retryable conflict, never
acknowledged as if that new payment had already been refunded. These recovery
policies are prerequisites to enabling purchases.

Refund revocation is durable and dominates later paid-event retries. It preserves
the accounting ledger and revokes customer access; it does not create a monetary
refund or reverse spent credits. Both identity headers and OAuth bearer tokens
observe revocation within the 30-second refresh bound. Paid authorization denies
access when configured storage cannot refresh revocations. Refund event status
and partial refunds must be distinguished before requesting irreversible access
revocation; the provider's successful benefits-revocation instruction is authoritative.
Polar documents [refund.created](https://polar.sh/docs/api-reference/2026-04/refund_created)
as independent of refund status and [order.refunded](https://polar.sh/docs/api-reference/2026-04/order_refunded)
as covering partial refunds too. The handler checks these facts explicitly.

Email runs after financial completion. Failed sends return a retryable server
error; the provider can redeliver using the same identity. Provider retries are
bounded, so email delivery is not guaranteed. The receipt's pending flag records
possible delivery work; no delivery queue or acknowledgment worker is implemented.
It must not be read as a successful-delivery count. Tokens and raw email addresses
are never stored in fulfillment receipts.

Run `SPINE_PG_TESTS=1 python -m pytest tests/integration/test_polar_fulfillment_pg.py`
against the disposable local PostgreSQL fixture. `SPINE_PG_IMAGE` selects an already
present image. CI explicitly pulls PostgreSQL 17, matching the production major,
and fails if this proof is skipped. The fixture covers concurrent transactions,
refund races, authorization, role privileges and the actual HTTP client/handler
contract. It does not call a payment provider or send customer email.

Deployment verification is read-only: inspect role/function/table privileges and
the scoped JWT's PostgREST schema, then verify the running build and payment switches.
Do not create synthetic production purchases to test the migration. Rollback may
leave the additive receipt table and private functions in place; retain the rows
and ledger for recovery. A code rollback does not undo completed transactions.
