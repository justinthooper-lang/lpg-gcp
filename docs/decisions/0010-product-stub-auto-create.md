# ADR-0010: Auto-create product stubs from order ingest

**Status:** Accepted
**Date:** 2026-06-01
**Supersedes:** the deferred decision flagged in [ADR-0005](./0005-order-items-fk-to-products.md)

## Context

The `shift4.order_items` table has a foreign key to `shift4.products(sku)`.
When a Shift4 `Order New` webhook arrives, the line items reference
SKUs that may not yet exist in our `shift4.products` table. This
creates a race condition between two independent webhook event streams:

- **Product events** (`Product New`, `Product Changed`, `Product Deleted`)
  populate `shift4.products`. We are not yet subscribed to these.
- **Order events** (`Order New`, `Order Status Change`) reference
  products by SKU. We are subscribed to `Order New`.

Even once we subscribe to product events, Shift4 makes no ordering
guarantee — an `Order New` for a freshly-created product could arrive
before the corresponding `Product New`. The order ingest path must
tolerate unknown SKUs.

ADR-0005 acknowledged this race and listed three candidate
resolutions: auto-create stub, retry, dead-letter queue (DLQ). The
choice was deferred to implementation time. Implementation is now done;
this ADR records the choice made and why.

## Decision

**Auto-create a stub `shift4.products` row** for every unknown SKU
encountered during order ingest, immediately before inserting the
line items, in the same transaction:

```sql
INSERT INTO shift4.products (sku, name)
VALUES (%s, %s)
ON CONFLICT (sku) DO NOTHING
```

The stub uses `ItemDescription` from the order item payload as the
placeholder `name`, falling back to the SKU itself if no description
is sent. `is_active` defaults to `TRUE` (column default).

When a real `Product New` or `Product Changed` webhook later arrives,
its upsert overwrites the stub with full product data: description,
retail_price, and the rest of the columns. The stub-vs-real
distinction is invisible to consumers; both are just rows in
`shift4.products`.

Implementation lives in `webhook-handler/ingest.py`,
function `ingest_order`, step 3.

### Alternatives considered

**Retry with backoff.** Catch the FK violation, sleep, retry. Rejected:
- Adds latency to the webhook response. Shift4 has a retry timeout;
  exceeding it triggers redelivery, which compounds the problem.
- Doesn't solve the case where the product genuinely doesn't exist
  yet (e.g., still being created in Shift4's admin).
- Adds complexity (state, backoff config, retry budget) for a
  case the stub approach handles for free.

**Dead-letter queue.** Catch the FK violation, write the payload to
a DLQ table or Pub/Sub topic, return 200 to Shift4, process the DLQ
later. Rejected:
- Heavier infrastructure for what is effectively a "data not arrived
  in expected order" problem — not a true failure.
- Operationally noisy: every order with any new SKU goes to DLQ on
  first webhook, then needs to be picked up. Most orders will
  contain a mix of known and new SKUs in normal operation.

**Reject the webhook with 500.** Let Shift4 retry. Rejected:
- Shift4's retry policy has a finite ceiling. Persistent rejection
  drops data.
- Same redelivery cost as the retry approach with even less control.

## Consequences

**Positive:**

- Order ingest never fails due to unknown SKUs. The FK is satisfied
  the moment we need it, in the same transaction.
- Pattern is idempotent: `ON CONFLICT DO NOTHING` makes re-running
  safe; replays don't create duplicates or override real product data.
- No new infrastructure (queues, retry workers, DLQ schemas).
- Matches the data shape Shift4 sends: every order item carries
  enough info (`ItemID`, `ItemDescription`) to populate the bare
  minimum stub.

**Negative:**

- `shift4.products` contains rows that are incomplete until a real
  Product webhook arrives. Consumers must tolerate
  `retail_price IS NULL`, generic `name` values, etc.
- The stub pattern conflates two semantic states ("product I know
  about from Shift4" vs "product I had to make up because it
  appeared in an order"). We could distinguish them via a
  `source` column or a `created_from_order` boolean if it ever
  matters operationally — currently it doesn't.
- If LPG never subscribes to Product webhooks, the stubs persist
  indefinitely. Acceptable — they have enough data to JOIN.

**Trade-off explicitly accepted:** we treat `shift4.products` as
"set of SKUs known to the system" rather than "complete product
catalog mirror." The schema doesn't enforce the latter, and the
distinction is invisible to most queries.

## Future work

- Subscribe to Shift4 Product webhooks once Layer 4 is deployed.
  Adds three more endpoints to `webhook-handler` and three more
  ingest paths.
- If product completeness matters for some queries, add a `is_stub`
  boolean (or compute it via `retail_price IS NULL`).

## References

- [ADR-0005](./0005-order-items-fk-to-products.md): original
  identification of the FK race
- Implementation: [`webhook-handler/ingest.py`](../../webhook-handler/ingest.py),
  step 3 inside `ingest_order`
