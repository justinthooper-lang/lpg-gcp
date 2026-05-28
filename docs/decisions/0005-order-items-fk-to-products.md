# ADR-0005: Webhook ingestion FK race conditions

**Status:** Accepted with caveats
**Date:** 2026-05-27 (extended 2026-05-28)

> **Note on filename:** This ADR originally only covered the
> `order_items → products` FK. It was extended on 2026-05-28 to also
> cover `orders → customers`, since the problem and resolution are
> identical. The filename is left as-is for link stability.

## Context

Two foreign keys in the `shift4.*` schemas create the same kind of
race condition with webhook ingestion:

- `shift4.order_items.sku` → `shift4.products.sku`
- `shift4.orders.shift4_customer_id` → `shift4.customers.shift4_customer_id`

In both cases, if a "child" webhook arrives before its "parent"
webhook (a new order before its product webhook, or a new order
before its customer webhook), the dependent insert fails with an FK
violation.

Shift4 does not guarantee delivery order across webhook types. Retry
logic, network issues, or genuinely out-of-order webhook firing can
all produce this.

The alternative — not having FKs — means:

- Typo'd or hallucinated values sneak in. Webhook payload bugs become
  silent data corruption.
- Joins return surprising results (missing rows on the parent side).
- "What products has SKU X been ordered as?" or "What orders has this
  customer placed?" become string searches rather than indexed joins.

## Decision

Enforce both FKs:

- `shift4.order_items.sku` → `shift4.products.sku`, `ON UPDATE CASCADE`
- `shift4.orders.shift4_customer_id` → `shift4.customers.shift4_customer_id`

Handle the race condition at the webhook handler layer. The strategy
will be one of:

1. **Auto-create stub** — on a child webhook, if the parent doesn't
   exist yet, insert a minimal parent row with only the ID populated.
   Other fields populated later when the actual parent webhook arrives
   or the next sync runs. **This is the leading candidate** — it's
   what most ecommerce systems do, and it makes ingestion lossless.
2. **Retry with backoff** — webhook handler catches FK violation,
   retries after delay, gives up after N attempts. Depends on Shift4's
   own retry behavior.
3. **Dead-letter queue** — failed inserts go to a DLQ topic for manual
   review. Heaviest but safest for unknown payloads.

The decision between these will be made when the webhook handler is
built and we know how Shift4 actually behaves under load. A follow-up
ADR will record the choice at that time.

## Consequences

**Positive:**

- Reads are simple and safe. `JOIN shift4.products USING (sku)` always
  works. `JOIN shift4.customers USING (shift4_customer_id)` always
  works. Reports can't accidentally include garbage references.
- Catalog and customer drift is detectable — a stub row with null
  human-readable fields is an obvious "this needs backfill" signal.
- `ON UPDATE CASCADE` on the SKU FK means renaming a SKU (if Shift4
  allows it) propagates correctly. Rare but possible.

**Negative:**

- Webhook handler must implement one of the strategies above before
  the schema is safe to use in production. **Until then, this ADR is
  partially honored — the FKs exist but no race-handling does.**
- Slight write overhead on every insert (FK check). Negligible.

**Open question:** the choice between the three strategies. Will be
decided when the webhook handler is built. Document the choice in a
new ADR at that time.
