# ADR-0023: Surrogate primary key for shift4.shipments

**Status:** Accepted
**Date:** 2026-06-16

## Context

`shift4.shipments` was created with `shift4_shipment_id` as a single-column
`PRIMARY KEY` (schema.sql; ADR-0002 denormalized shipping address). The
assumption was that Shift4's `ShipmentID` uniquely identifies a shipment.

It does not — at least not at order-creation time. Shift4 sends `ShipmentID = 0`
for **every** order when the webhook fires; a real shipment id is only assigned
later, if and when a shipment is actually created in Shift4. The ingest
(`ingest_order`) writes one shipment row per order at creation, so:

- the first real order inserted a shipment row with `shift4_shipment_id = 0`,
- every subsequent order also carried `ShipmentID = 0` and **collided** on the
  primary key (`SQLSTATE 23505`, `duplicate key value violates unique constraint
  "shipments_pkey"`).

Because the entire order ingests in one transaction, the collision rolled the
**whole order back**. The webhook returned 500; Shift4 retried; the retries also
500'd. Net effect: after the first real order, **every order was silently
dropped** while the webhook had been registered and "working." The test fixtures
used in development had unique, non-zero shipment ids, so the path passed in
tests and only failed against live traffic. (Diagnosed 2026-06-16 from
structlog `order_ingest_db_error` events in Cloud Logging — the access logs only
showed bare 500s; the real exception was in `jsonPayload.exception`.)

Two fixes were considered:

1. **Composite key `(shift4_order_id, shift4_shipment_id)`.** Closer to "natural"
   keying, but still breaks if a single order ever carries two shipments both with
   `ShipmentID = 0` (Shift4 supports multi-ship-to orders; at creation they would
   all be 0) — the same collision, now *within* one order.
2. **Surrogate identity primary key.** Decouples row identity from Shift4's value
   entirely.

## Decision

**Re-key `shift4.shipments` with a surrogate `id BIGINT GENERATED ALWAYS AS
IDENTITY PRIMARY KEY`, and demote `shift4_shipment_id` to a plain, non-unique
`NOT NULL` data column.** (Migration `0008_shipments_surrogate_pk.sql`.)

The real idempotency key is already `shift4_order_id`: the ingest does
`DELETE FROM shift4.shipments WHERE shift4_order_id = %s` before re-inserting, so
re-ingesting an order cleanly replaces its shipment rows. The surrogate key just
gives each row a unique identity; `shift4_shipment_id` retains Shift4's value
(`0` at creation, a real id later if a shipment is created and the order
re-ingests) as informational data. Many orders may now share
`shift4_shipment_id = 0` without collision.

The migration also **drops the vestigial `shift4.order_items.shift4_shipment_id`
column** — a foreign key to the old shipments primary key that the ingest never
populated and nothing ever read. It existed only as a schema relationship that
real data never exercised; dropping it removes the dependency that would
otherwise block the primary-key change and deletes dead schema rather than
working around it.

**No application code change accompanied this.** The running handler (`v0.20.0`)
already omitted `shift4_shipment_id` from its `order_items` INSERT and inserted
`shift4_shipment_id` into shipments as a plain value, so the live service became
correct the moment the unique constraint was dropped — migration-only, no
redeploy.

## Consequences

- New-order ingest works for real Shift4 traffic; orders no longer drop on the
  shipment collision. Verified end-to-end 2026-06-16: a webhook Test POST
  returned 200 with `order_ingested` (2 items, 2 shipments), and the 2026
  backfill loaded 312 orders / 314 shipments with zero primary-key collisions.
- `shift4_shipment_id` is no longer unique. Any future code that needs to address
  a specific shipment must use the surrogate `id` (or `shift4_order_id` +
  business logic), not `shift4_shipment_id`. Reads today are all by
  `shift4_order_id` (e.g. `ORDER BY shift4_shipment_id`), which is unaffected.
- The line-item → shipment association (dropped FK column) is gone. It was never
  used; if a per-line shipment link is ever needed, it should be designed against
  the new surrogate key and actually populated by the ingest.
- schema.sql updated to match so the source-of-truth file reflects the live shape.

## Future work

- Delete the synthetic webhook Test order (`shift4_order_id = 1`, placeholder
  2016 date) before it reaches any reporting.

## References

- Migration: `migrations/0008_shipments_surrogate_pk.sql`
- ADR-0002 (denormalized shipping address), ADR-0009 (Shift4 webhook contract)
- Bug diagnosis + fix session: 2026-06-16
