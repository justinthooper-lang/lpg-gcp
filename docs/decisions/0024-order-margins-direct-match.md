# ADR-0024: Order-level margins via direct order↔invoice match

**Status:** Accepted
**Date:** 2026-06-17

## Context

The dashboard needs true per-order **profit** and **shipping differential**, both
of which require the actual Crown cost and freight that live on
`lpg.vendor_invoices`. The originally-designed path was a three-way match
`orders → purchase_orders → vendor_invoices`, joining the PO to the invoice on
`purchase_orders.po_number = vendor_invoices.customer_po_number`.

Two facts made that path the wrong dependency for margins:

1. **The PO is not the source of the join key.** `purchase_orders.po_number` is
   derived from the order's `invoice_number` (the `PO#####` the storefront
   assigns), and Crown prints that same value as `customer_po_number` on its
   invoice. So the real match key is `orders.invoice_number =
   vendor_invoices.customer_po_number`. The PO row merely *carried* that key.

2. **Historical orders have no PO and won't get one.** We backfilled 312 historical
   2026 orders (ADR refs / 2026-06-16) but deliberately do **not** generate POs for
   already-shipped historical orders. Requiring a PO would leave every historical
   order with NULL margins forever.

Separately, the actual historical cost/freight data did not exist in GCP: only ~16
Crown invoices had been ingested. LPG's prior system (Salesforce) had recorded the
real Crown supplier cost and freight per order; a Salesforce export
(`report1781714157422.csv`, 320 rows) carried `Supplier Invoice Total`,
`Supplier Actual Shipping`, and the `Shift4Shop Order Number` (the PO# join key),
plus Salesforce's own computed `Order Profit` / `Shipping Differential` (usable as
an independent cross-check).

A data-integrity hazard also surfaced while validating: synthetic test orders
(low `shift4_order_id`, e.g. `1`/AB-1000 and `31990`) were squatting on real
`invoice_number` values (`PO31990`), colliding with the real order and producing
a false loss when the margin view joined the invoice to the wrong row.

## Decision

1. **Match margins directly: `orders.invoice_number =
   vendor_invoices.customer_po_number`, with no `purchase_orders` dependency.**
   Profit = customer total − supplier cost − actual freight; shipping
   differential = customer shipping − actual freight. The PO-mediated three-way
   match remains available for *expected-vs-actual* variance on forward orders,
   but is not on the margin path.

2. **Migrate historical Crown actuals from the Salesforce export into
   `lpg.vendor_invoices`** (`scripts/backfill_vendor_invoices_from_sf.py`), only
   for rows that carry a real supplier cost (272 of 320; the 48 un-invoiced drafts
   are skipped so they never get a fabricated ~100% margin). Migrated rows are
   marked for provenance — `graph_message_id = 'sf-migration:<PO>'`,
   `vendor_invoice_number = 'sf:<PO>'` — and are **backstops**: when the real Crown
   invoice for the same PO is later ingested via the daily sync, the margin view
   prefers it.

3. **Express margins as a view, `lpg.v_order_margins`** (migration
   `0009_order_margins_view.sql`), which picks the best invoice per order (real
   Crown over `sf:` backstop), sums truck+UPS freight (orders ship one or the
   other), and yields NULL margins with `has_invoice = false` where no invoice
   exists — never a fabricated number.

4. **Purge synthetic test orders** from `shift4.orders` (and their
   `order_items`/`shipments` children) so they cannot collide with real
   `invoice_number`s. Identified by non-production `shift4_order_id` (real Shift4
   ids are ~6 digits; tests were `1`, `31990`).

## Consequences

- Historical orders gain true margins without PO generation: of 314 2026 orders,
  **284 have invoice-true margins**, 30 are "cost pending" (NULL, segmented via
  `has_invoice`). YTD profit ≈ $52.3K on ≈ $166K revenue (~31%); 53 orders
  undercharged shipping (`shipping_differential < 0`) — the dashboard's headline
  metric.
- Validated against the source: PO31990 computes profit $392.46 / shipping-diff
  $29.91, matching both the Salesforce export and the actual Crown invoice PDF
  (226714) to the cent.
- Join-key format is consistent across all three sources (verified 2026-06-17 by
  querying stored values): `shift4.orders.invoice_number` is `PO`-prefixed in
  314/314 rows; `lpg.vendor_invoices.customer_po_number` is `PO`-prefixed in all
  287 rows — both the 272 SF-migrated rows AND the 15 real Crown-ingested rows.
  An earlier draft of this ADR worried that real Crown invoices stored a bare
  number (`31990`) because the invoice's human-readable label reads that way, but
  the Crown parser regex (`\b(PO\d+)\b`) captures the `PO`-prefixed token from
  the PDF text, so stored values agree. **No normalization is required**, and live
  Crown invoices will match orders and supersede the SF backstops as designed.
- SF-migrated rows are a one-time historical import, not Crown-PDF-sourced; the
  `sf-migration:` / `sf:` markers keep the two provenances distinguishable.

## Future work

- Optionally fold expected-vs-actual variance (PO cost vs invoice cost) into the
  view for forward orders that do have POs.

## References

- View: `migrations/0009_order_margins_view.sql`
- Loader: `scripts/backfill_vendor_invoices_from_sf.py`
- Source export: Salesforce `report1781714157422.csv` (272 cost-bearing rows)
- ADR-0018 (PO generation), ADR-0021 (order overrides), ADR-0009 (Shift4 contract)
- Validation: 2026-06-17 (PO31990 vs Crown invoice 226714)
