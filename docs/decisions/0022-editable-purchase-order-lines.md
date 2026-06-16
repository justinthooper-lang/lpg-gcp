# ADR-0022: Editable draft purchase-order lines

**Status:** Accepted
**Date:** 2026-06-16

## Context

ADR-0021 made the order *header* (ship-to, billing, comments) correctable via an
overlay on the read-only `shift4.orders` mirror. In use it became clear the real
operational need is correcting **what gets ordered from Crown** — the line items:
fixing a quantity, removing a line Crown can't fulfil, and **adding lines that
never existed on the storefront order at all** (a substitute SKU, an extra item,
a manual fee).

Order line items live in `shift4.order_items`, a storefront mirror under the same
source-of-truth rule as the order header (ADR-0001): webhook writes only. So the
ADR-0021 instinct — overlay the mirror — could be extended here. But line items
are not a one-row header: they are a **1:many collection**, and the need includes
**adds and deletes**, not just field corrections. A COALESCE overlay models a
field correction cleanly; it does not model "this storefront line is deleted" or
"this line has no storefront counterpart" without a soft-delete flag, a separate
additions table, line-identity keying, and merge logic — real complexity.

Crucially, there is already an LPG-owned, writable, per-line document built for
exactly this: the **purchase order**. `lpg.purchase_orders` +
`lpg.purchase_order_lines` (ADR-0018, migration 0004) are LPG data — generated as
a `draft` from the order, snapshotting each line (`vendor_sku_code`,
`description`, `quantity`, `unit_cost`, or a fee `amount`), with a shape guard
keeping product and fee lines honest. A PO is, by nature, a draft you adjust
before issuing.

## Decision

**Make the draft purchase order the editable surface. Add/edit/delete lines
directly on `lpg.purchase_order_lines`; leave `shift4.order_items` (and the
order) a pristine mirror.** This matches standard procurement practice — generate
a draft from the sales order, then correct the draft before sending to the
vendor — and keeps every correction in LPG-owned data, with no overlay machinery.

### Operations (admin-only, draft-only)

Three endpoints on `lpg-admin` (IAM-gated, 404 on the public webhook-handler,
like the existing PO endpoints):

- `POST   /purchase-orders/{po_number}/lines` — add a line.
- `PATCH  /purchase-orders/{po_number}/lines/{line_id}` — edit a line.
- `DELETE /purchase-orders/{po_number}/lines/{line_id}` — delete a line.

Each respects the `chk_purchase_order_lines_kind` shape guard: a **product line**
carries `vendor_sku_code` + `quantity` + `unit_cost` (no `amount`); a **fee line**
carries `description` + `amount` (no product fields). `quantity > 0` and costs
`>= 0` are already enforced at the DB level and surfaced as 4xx, not 500.

### Policy 1 — sent POs are immutable

Edits and regeneration are refused once `status = 'sent'` (409). A sent PO is the
audit record of what was emailed to Crown; it does not change after the fact. This
also closes the open note ADR-0018 left — that regeneration could currently clobber
a sent PO. To revise a sent PO, regenerate explicitly (which mints a fresh draft).

### Policy 2 — manual edits are protected from regeneration

`generate_purchase_order` rebuilds lines by `DELETE`-ing them and re-deriving from
the order. Migration 0007 adds `lpg.purchase_orders.manually_edited`, set true on
any hand edit. Regeneration **refuses** (409) to overwrite a `manually_edited`
PO unless explicitly forced (`?force=true`); a forced regeneration replaces the
lines and resets the flag to false. In the UI the "Generate PO" button becomes
"Regenerate from order (discards your edits)" with a confirm once edits exist — a
stray click cannot silently destroy work, but the re-seed escape hatch remains.

### Rendering

No new render path: the PDF and total already render on demand from the stored
lines (`load_purchase_order` → `render_purchase_order_pdf`), so the preview and
total refresh by re-fetching after each edit.

## Alternative considered: overlay order line items (extend ADR-0021)

Build `lpg.order_item_overrides` (field corrections + a soft-delete flag) and
`lpg.order_item_additions` (net-new lines), unioned into a
`lpg.v_order_items_effective` view that PO generation reads. **Rejected for this
goal.** It is materially more complex than the header overlay — line identity,
soft-delete, net-new rows, and merge logic — to produce a corrected *order*, when
the thing that actually needs to be correct is the *PO*. The PO is already the
editable LPG artifact; routing edits through an order overlay only to regenerate
them onto the PO adds a layer without adding value.

This alternative would be the right call only if LPG needed a corrected *order
record* independent of any PO — e.g. for margin/reconciliation reporting that must
reflect "what we actually fulfilled" at the order grain. That need does not exist
today. If it arises, this ADR does not preclude it: the order mirror is untouched,
so an overlay can be added later without unwinding anything here.

### Relationship to ADR-0021

ADR-0021's order-header overlay stays. It still feeds ship-to into PO generation,
and it is harmless. Note that with editable POs the PO's own ship-to fields
(`lpg.purchase_orders.ship_*`) become directly editable too, which makes the
ADR-0021 editor somewhat redundant *for the PO workflow* — retiring it is a
possible later cleanup, deliberately deferred, not done here.

## Consequences

**Positive:**

- The order and `shift4.order_items` stay faithful storefront mirrors; no overlay
  for a 1:many collection. Corrections live in LPG-owned PO rows.
- Matches procurement norms (edit the draft PO before issuing) — defensible and
  transferable, the project's reference-architecture goal.
- Tightens an existing loose end: sent POs become immutable (ADR-0018's open note).
- Reuses the existing render path; the shape guard and quantity/cost CHECKs give
  data integrity for free.

**Negative:**

- A hand-edited PO and its source order can diverge — intentional, but it means the
  PO is no longer a pure function of the order. `manually_edited` makes that state
  explicit and visible, and forced regeneration is the reset.
- Edits live only on the PO; they do not flow back to the order or to any
  order-grain report. Acceptable given the goal; revisit if order-grain reporting
  is built (see the rejected alternative).
- `lpg-admin` gains more mutating endpoints (line CRUD). Consistent with ADR-0015's
  acknowledged shift away from read-only; the compute SA already holds `lpg.*` DML
  (ADR-0012).

## Future work

- Build steps: line CRUD endpoints → editable composer line table → deploy.
- Optionally retire the ADR-0021 header editor once PO editing is proven, or keep
  it as the order-grain correction surface.
- Fold the PO tables (migrations 0004/0007) into `schema.sql` — they currently
  live only in migrations, so `schema.sql` is not the complete source of truth it
  claims to be. A separate doc-hygiene task.

## References

- Migration: [`migrations/0007_purchase_order_manual_edits.sql`](../../migrations/0007_purchase_order_manual_edits.sql)
- PO schema: [`migrations/0004_purchase_orders.sql`](../../migrations/0004_purchase_orders.sql)
- Generate/regenerate + send logic: [`webhook-handler/purchase_order_repository.py`](../../webhook-handler/purchase_order_repository.py)
- PO endpoints + composer: [`webhook-handler/main.py`](../../webhook-handler/main.py), [`webhook-handler/po_composer.py`](../../webhook-handler/po_composer.py)
- Related ADRs:
  [ADR-0018](./0018-purchase-order-generation.md) (PO generation — extended here),
  [ADR-0021](./0021-order-field-overrides.md) (order-header overlay — companion),
  [ADR-0015](./0015-split-webhook-and-admin-services.md) (admin service gains mutating endpoints),
  [ADR-0012](./0012-iam-database-auth.md) (`lpg.*` DML grants cover these writes)
