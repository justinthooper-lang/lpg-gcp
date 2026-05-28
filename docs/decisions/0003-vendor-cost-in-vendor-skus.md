# ADR-0003: Vendor cost lives on `lpg.vendor_skus`, not on products

**Status:** Accepted
**Date:** 2026-05-27

## Context

We need to track vendor cost for every product LPG sells. The naive
place to put it is on the product itself — e.g., `shift4.products`
gains a `vendor_cost` column.

This creates two problems:

1. **It pollutes the Shift4 mirror.** `shift4.products` is supposed to
   reflect what's in the storefront. Vendor cost is LPG's data; Shift4
   doesn't know it and doesn't care. Putting it on `shift4.products`
   muddies the source-of-truth boundary (see ADR-0001).
2. **It assumes products are 1:1 with vendor SKUs.** Many LPG products
   are kits — a globe assembly might be one body, one finial, and one
   mounting kit, possibly from different vendors. A single `vendor_cost`
   column can't represent this.

The operational reality is also that products are entered in Shift4
(by people on the storefront side), but vendor cost is maintained by
LPG and **has historically gone stale** because there's nowhere clean
to maintain it.

## Decision

Vendor cost lives on `lpg.vendor_skus.unit_cost`. A vendor SKU is
defined as: one thing LPG can buy from one vendor at one price.

Customer-facing products (`shift4.products`) connect to vendor SKUs
through the bill-of-materials table (see ADR-0004), not directly. To
compute the COGS of a product, sum the vendor SKU costs weighted by
BOM quantity.

`shift4.products` does **not** have a `vendor_cost` column. If you
find yourself wanting to add one, stop and reread this ADR.

## Consequences

**Positive:**

- Source-of-truth stays clean. Shift4 owns products; LPG owns vendor
  data.
- Kit products work correctly. A product assembled from 3 vendor SKUs
  has its cost calculated correctly from the components.
- Same vendor SKU used across multiple products is maintained in one
  place. Update the cost once, every product that uses it picks it up.
- Multi-vendor sourcing for the same physical item becomes possible
  later — you'd have two `vendor_skus` rows pointing to the same
  underlying thing, and BOM logic picks one based on price/availability.

**Negative:**

- "What does this product cost us?" is no longer a single column lookup.
  It's a query: join product → BOM → vendor_skus, sum the components.
  Worth writing as a view (`lpg.product_cogs_v` or similar) once the
  tables are populated.
- More setup work — every product needs at least one BOM row to be
  costable. Acceptable; this is also a forcing function to keep the
  BOM accurate.
