# ADR-0004: Represent kits via a bill-of-materials table

**Status:** Accepted
**Date:** 2026-05-27

## Context

Many LPG products are kits: a customer buys one SKU on the storefront,
but LPG fulfills it by pulling multiple physical items off the shelf.
A globe assembly might be `globe body + finial + mounting hardware`,
each from a potentially different vendor.

We need to model:

- What components make up a customer-facing product
- How many of each component
- Which vendor each component comes from
- The cost of each component (so we can compute COGS)

There are three common patterns:

1. **Flatten** — duplicate product rows per component. Awful, breaks
   everything.
2. **Self-referencing product table** — `parent_sku` column on
   `products`. Works for simple hierarchies but conflates "the thing
   we sell" with "the thing we buy."
3. **Separate BOM table** — explicit mapping from customer SKU to
   vendor SKUs with quantities. Standard manufacturing pattern.

## Decision

Use a separate BOM table: `lpg.product_components`. Columns:

- `product_sku` — FK to `shift4.products.sku` (the thing customers buy)
- `vendor_sku_id` — FK to `lpg.vendor_skus` (the thing LPG buys)
- `quantity` — how many of this component per one product
- `sort_order` — for display purposes (kit instructions, packing lists)
- `notes` — free-form, for assembly quirks

Unique constraint on `(product_sku, vendor_sku_id)` so a component
appears at most once per product. If you need "two of these and three
of those," that's `quantity = 2` and `quantity = 3`, not two rows.

## Consequences

**Positive:**

- Single-component products and multi-component kits use the same
  pattern. A "simple" product is just a product with one BOM row at
  `quantity = 1`.
- COGS calculation is uniform: always sum `vendor_skus.unit_cost *
  product_components.quantity` across the BOM.
- Packing lists and pick lists fall out of the same data.
- Substituting a component (different vendor, same physical item) is
  an update to one BOM row, not a refactor.

**Negative:**

- Slight indirection for the simplest case (a non-kit product still
  needs a BOM row). Worth it for uniformity.
- We have to enforce "every sellable product has at least one BOM row
  to be costable" at the app layer or via a view. The schema alone
  can't express "must have one or more children." Acceptable.
- If a kit's components change over time, we lose history of old BOM
  configurations unless we add `effective_date` columns. **Open
  question — not solved yet.** May matter for old POs that referenced
  the historical kit composition.
