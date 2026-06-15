# ADR-0021: Order field corrections via an LPG-owned override overlay

**Status:** Accepted
**Date:** 2026-06-15

## Context

LPG needs to correct fields on ingested orders for back-office workflows — the
immediate driver is orders that arrive with a missing or garbled ship-to, which
degrades the generated PO to "(no ship-to on order)" (a standing BACKLOG watch
item). The natural request is "make the order fields editable."

But orders live in `shift4.orders`, which is a **mirror of Shift4Shop** — and
Shift4 is the system of record. The project's single most important rule
(ADR-0001, architecture source-of-truth rule #1) is that `shift4.*` is written
**only** by the webhook handler; nothing else writes it. Editing those columns
in place breaks that rule, and two concrete failure modes follow from the
ingest code:

1. **Silent clobber.** `ingest_order` upserts with `ON CONFLICT (shift4_order_id)
   DO UPDATE` ([`webhook-handler/ingest.py`](../../webhook-handler/ingest.py)).
   A re-fired webhook for an edited order overwrites `order_status`, all totals,
   and `raw_payload` from the payload — an in-place edit to those fields vanishes
   with no error, the worst kind of bug.
2. **Undetectable divergence.** The conflict clause happens not to touch the
   address columns today, so an address edit would *survive* re-ingest — but only
   by accident of the current upsert. The local DB would then disagree with the
   storefront with no record that it was changed or why.

This is the same problem Salesforce solves for synced external data: you don't
hand-edit the synced field, you add your own field beside it and overlay at read
time.

## Decision

**Keep `shift4.orders` a pristine mirror. Put LPG's corrections in a new
LPG-owned table, `lpg.order_overrides`, and overlay it onto the mirror at read
time through a view, `lpg.v_orders_effective` (COALESCE override over mirror).**

### Data model

- `lpg.order_overrides` — PK/FK `shift4_order_id → shift4.orders` (`ON DELETE
  CASCADE`), one row only when a correction exists. Nullable override columns for
  the billing address, ship-to address, and `comments`. Provenance columns
  `override_reason` and `overridden_by` make every correction auditable, plus the
  standard `created_at`/`updated_at` (with the existing `lpg.set_updated_at`
  trigger). A NULL override column means "no override" — fall back to the mirror.
- `lpg.v_orders_effective` — `shift4.orders LEFT JOIN lpg.order_overrides`,
  `COALESCE`-ing the overridable columns and passing the rest through. A
  `has_override` boolean flags rows that carry a correction. Read paths that need
  the corrected value (the PO builder in
  [`purchase_order_repository.py`](../../webhook-handler/purchase_order_repository.py),
  the admin order-detail page) read this view instead of `shift4.orders`
  directly, so corrections flow into POs automatically.

### Scope: what is overridable

Addresses, contact fields, and `comments` only. **`order_status` and the
monetary totals (`subtotal`, `tax`, `shipping_cost`, `discount`, `grand_total`)
are deliberately not overridable.** The totals are mirrored expressly to
reconcile customer charges against Crown invoices; a local override would corrupt
that reconciliation. A disputed total is a reconciliation note, not an order
edit. If a genuine need to override totals appears later, it gets its own ADR and
its own (clearly separate) mechanism.

### Alternative considered: make `shift4.orders` columns directly editable

The straightforward "add an UPDATE form over the mirror table." Rejected: it
violates source-of-truth rule #1, is subject to silent clobber on re-ingest
(failure mode 1 above), and produces an unaudited divergence between the local
mirror and the storefront (failure mode 2). It is the kind of shortcut that reads
fine until the day Shift4 re-fires a webhook. The overlay costs one small table
and one view and has none of these properties.

### Alternative considered: push the correction back to Shift4Shop

For a value that should change in the *order of record* (the customer's actual
address), the correct place is Shift4Shop, and it would flow back via webhook.
But that is a different use case. Here LPG needs a *local* correction for a
back-office artifact (the PO) without asserting that the storefront order itself
was wrong — and often without write access to that order at all. The overlay
serves the back-office need without touching the system of record.

## Consequences

**Positive:**

- Source-of-truth rule #1 is preserved: `shift4.orders` stays webhook-only and
  faithful to Shift4. Re-ingest can never clobber a correction — the upsert never
  touches `lpg.*`.
- Fully reversible: delete the override row and the order reverts to storefront
  truth, with nothing lost.
- Auditable and explainable: every correction records who and why, and
  `has_override` plus the mirror-vs-effective split makes a change visible. The
  pattern hands off to a client unchanged.
- Corrections flow into POs by swapping one `FROM shift4.orders` to
  `FROM lpg.v_orders_effective`; no change to ingest or the upsert.

**Negative:**

- A second place to look for an order's effective field values (mirror vs.
  overlay). The view is the mitigation — read paths go through one surface.
- Read paths must be migrated to the view to benefit; any path still reading
  `shift4.orders` directly sees the un-corrected value. The PO builder is the
  first and most important migration.
- The overlay is per-field COALESCE, so an override cannot set a field *back* to
  NULL/blank when the mirror is non-NULL (NULL means "no override"). No current
  use case needs blanking; if one appears it warrants a sentinel design and an
  ADR amendment.

## Future work

- Wire `lpg.v_orders_effective` into the PO builder read and the admin
  order-detail read.
- Add the edit form to the `lpg-admin` order-detail page, writing only to
  `lpg.order_overrides`, showing storefront-vs-override side by side.
- Backfill the missing ship-to on existing orders that need POs (closes the
  BACKLOG watch item).

## References

- Migration: [`migrations/0006_order_overrides.sql`](../../migrations/0006_order_overrides.sql)
- Schema (source of truth): [`schema.sql`](../../schema.sql)
- Ingest upsert (the clobber path): [`webhook-handler/ingest.py`](../../webhook-handler/ingest.py)
- PO read to migrate: [`webhook-handler/purchase_order_repository.py`](../../webhook-handler/purchase_order_repository.py)
- Related ADRs:
  [ADR-0001](./0001-shift4-lpg-schema-split.md) (the schema split this preserves),
  [ADR-0002](./0002-address-denormalization.md) (why addresses live on the order),
  [ADR-0009](./0009-shift4-webhook-contract.md) (ship-to denormalization at ingest),
  [ADR-0018](./0018-purchase-order-generation.md) (PO generation that consumes ship-to)
