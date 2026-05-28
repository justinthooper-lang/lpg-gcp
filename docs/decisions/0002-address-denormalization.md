# ADR-0002: Denormalize addresses on orders and shipments

**Status:** Accepted
**Date:** 2026-05-27

## Context

Customers at LPG commonly have multiple addresses — both shipping and
billing, and shipping addresses that vary order-to-order (different
offices, different job sites, gifts to third parties). A given order's
shipping address will not always match the customer's "primary" address.

There are two common ways to model this:

1. **Normalized:** an `addresses` table with a foreign key to customer.
   Orders and shipments reference address rows by ID.
2. **Denormalized:** address fields live directly on `orders` (billing)
   and `shipments` (shipping). The customer table holds at most a
   "default" address for convenience.

## Decision

Denormalize. Billing address fields live on `shift4.orders`. Shipping
address fields live on `shift4.shipments`. No central `addresses` table.

## Consequences

**Positive:**

- The address on an order is **historically accurate forever.** If a
  customer moves and we re-edit their address in a normalized model,
  every old order's address changes too — which is wrong for billing
  records, returns, and audit. Denormalization gives us snapshot
  semantics for free.
- Matches how Shift4 actually sends data via webhook — addresses are
  embedded in the order/shipment payload, not referenced by ID.
- Simpler queries. Order details don't require joins to render.

**Negative:**

- More columns on `orders` and `shipments` (10+ address fields each).
  Acceptable.
- If a customer updates their address in Shift4, future orders get the
  new address but historical orders keep the old one. This is the
  desired behavior, but worth flagging — if anyone expects "the
  customer's current address" they should query Shift4 / a future
  `customer_default_address` field, not the latest order.
- Address validation / normalization (USPS lookups, etc.) gets
  duplicated work. Acceptable for now; revisit if we ever do
  validation server-side.

**Trade-off explicitly accepted:** we are choosing historical accuracy
over storage efficiency. Disk is cheap, audit confusion is expensive.
