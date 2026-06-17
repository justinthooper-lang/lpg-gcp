# ADR-0025: Manual margin entry for orders without a Crown invoice

**Status:** Accepted
**Date:** 2026-06-17

## Context

`lpg.v_order_margins` (ADR-0024) computes true profit and shipping differential
for orders matched to a Crown invoice, and leaves them NULL otherwise. Of the 314
backfilled 2026 orders, ~30 have no matched invoice (`margin_source = 'none'`) —
recent orders Crown hasn't invoiced yet, or orders whose invoice was never
ingested. Those orders show no margin and don't contribute to dashboard profit.

The operator needs to be able to fill that gap — enter the supplier cost and
freight by hand for an order that lacks an invoice — so it gets a margin and
counts in the dashboard. But hand-entered numbers must never undermine the
invoice-true data the system is built on: a manual figure must not override a real
Crown invoice, and the dashboard must remain able to distinguish invoice-backed
profit from hand-entered profit.

An earlier framing considered a general "override" (manual value wins over the
computed value). That was rejected: overriding a matched invoice would let a typo
silently corrupt real margins, and it breaks the "invoice is the source of truth"
guarantee.

## Decision

**Manual entry is a gap-filler, not an override.** It applies only when an order
has no matched invoice; a real invoice always wins.

1. **`lpg.order_margin_manual`** (migration `0010`): one row per order,
   `manual_supplier_cost` + `manual_freight` (both required to form a margin),
   optional `note`. Keyed by `shift4_order_id`. Absence of a row = nothing
   entered.

2. **Precedence in `v_order_margins`** (rebuilt in `0010`):
   `margin_source = 'invoice'` (matched Crown invoice) > `'manual'` (manual entry,
   only when no invoice) > `'none'`. The effective `supplier_cost` / `actual_freight`
   come from the winning source, and `profit` / `shipping_differential` always
   **recompute** from that basis (`profit = grand_total − supplier_cost −
   actual_freight`; `shipping_differential = shipping_cost − actual_freight`), so
   the arithmetic stays internally consistent regardless of source.

3. **Write path refuses to override an invoice.** `POST /orders/{id}/margin`
   (lpg-admin only) returns 409 if the order's `margin_source` is already
   `'invoice'`. It requires *both* cost and freight (a partial entry can't form a
   margin); a fully-blank body clears the manual row.

4. **Manual entries are retained, not deleted, when superseded.** If a real Crown
   invoice arrives later via the daily sync and matches the order, the view's
   precedence makes the invoice win automatically; the manual row simply stops
   being used (kept for audit / in case the match is later removed).

5. **The dashboard counts manual margins** in profit totals (the operator's stated
   intent) but tracks `margin_source` so the invoice-true vs manual vs pending
   split stays visible — same transparency principle as the existing matched /
   pending split.

## Consequences

- Orders without an invoice can be given a margin by hand and will appear in
  dashboard profit, with the basis flagged as `manual`.
- Invoice-true margins are protected: the API refuses to write over a matched
  invoice (409), and the view never lets a manual value beat an invoice.
- `margin_source` is now the single field that says where any order's margin came
  from; UI and dashboard branch on it (`invoice` → read-only; `none`/`manual` →
  editable).
- The order detail page gains an "Economics" section (read-only when
  invoice-matched, editable otherwise) via `order_economics_form.py`.

## Future work

- Surface the `invoice / manual / none` split on the dashboard cards so the
  proportion of hand-entered profit is visible at a glance.

## References

- Migration: `migrations/0010_order_margin_manual.sql`
- API: `webhook-handler/main.py` (`get_order_margin` / `save_order_margin`)
- UI: `webhook-handler/order_economics_form.py`
- Builds on ADR-0024 (direct order↔invoice margin match), ADR-0021 (order overrides pattern)
