# ADR-0009: Shift4 webhook contract and resulting schema additions

**Status:** Accepted
**Date:** 2026-05-29

## Context

The lpg-gcp project is building a Shift4Shop webhook handler that
ingests `Order New` events into the `shift4.*` schema. Before writing
the Pydantic model and the handler, we needed concrete answers to
three open questions about the actual webhook payload shape:

1. Is `CustomerID` an integer or a string with a `guest-` prefix?
2. What integer values does `OrderStatusID` use, and how do they map
   to the textual order statuses LPG cares about (New, Processing,
   Shipped, Quote)?
3. How are subtotal and shipping cost represented?

Initial sources investigated:

- Shift4Shop's official knowledge base confirmed the webhook body
  format matches the REST API GET response for the resource
  ([source](https://support.3dcart.com/Knowledgebase/Article/View/791/14/what-are-webhooks-and-how-are-they-used)).
- The REST API order schema documents every field
  ([source](https://apirest.3dcart.com/v2/orders/index.html)) but
  doesn't reveal which Shift4 quirks the LPG-specific data exhibits.

The decisive source: **LPG's existing Shift4Shop → Salesforce sync
script** (`sync/sf_sync.py` from the older
[LampPostGlobes_CRM](https://github.com/justinthooper-lang/LampPostGlobes_CRM)
project). That script has been running against real LPG data and
encodes hard-won knowledge about Shift4's behavior.

## Decision

### Answers to open questions

**1. CustomerID type.** Shift4 sends `CustomerID` as an integer (or
string-of-integer). Value `0` or empty indicates a guest checkout.
The `guest-XXXXX` prefixed strings observed in the existing
Salesforce data are **synthesized by the integration code** as
`f"guest-{OrderID}"` — Shift4 does not send them. Our schema's
`shift4.customers.shift4_customer_id TEXT` PK accommodates both
formats correctly. The webhook handler will synthesize the
`guest-{OrderID}` form for guest customers at ingest.

**2. OrderStatusID → text mapping.** Only four status IDs are in
active use at LPG:

| OrderStatusID | Text status |
|---|---|
| 1  | New |
| 2  | Processing |
| 4  | Shipped |
| 21 | Quote |

The integer `3` is not in active use (likely "Partial"). Any status
not in this list is filtered at the webhook layer and not ingested.
Additionally, `Quote` (21) is filtered at the webhook layer per a
business rule (quotes are not real orders); the
`chk_orders_status_not_quote` CHECK constraint on `shift4.orders`
provides a database-level safety net for the same.

**3. Shipping cost and subtotal.** Shift4 does not send a `subtotal`
field. Shipping cost comes from one of:
- Sum of `ShipmentList[].ShipmentCost` if shipments exist
- `o.InvoiceShipping` field as fallback
- Zero otherwise

The webhook handler will compute these at ingest:
- `shift4.orders.shipping_cost`: sum of `ShipmentList[].ShipmentCost`
  with `InvoiceShipping` fallback
- `shift4.orders.subtotal`: sum of `quantity × unit_price` across
  `OrderItemList`
- `shift4.orders.tax`: sum of `SalesTax` + `SalesTax2` + `SalesTax3`
- `shift4.orders.discount`: `OrderDiscount` field as-is
- `shift4.orders.grand_total`: `OrderAmount` field as-is

### Schema additions (migration 0001)

The investigation revealed three gaps in our existing `shift4.orders`
table that the webhook payload exposes:

**`invoice_number TEXT`** — Shift4 sends `InvoiceNumberPrefix` and
`InvoiceNumber` separately (e.g., `"PO" + 31990`). Concatenated, this
is the human-readable order identifier customers use, the value that
appears on Crown invoices, and the value LPG operations references
day-to-day. The internal `shift4_order_id` is a separate numeric ID
useful for joins but invisible to humans.

**`comments TEXT`** — Shift4's `Comments` field holds customer-entered
text at checkout (special instructions, gift messages). While
`raw_payload` JSONB captures everything Shift4 sends, querying JSONB
for "find orders with special instructions" is awkward. A dedicated
column makes the field queryable and indexable.

**`ship_to_* TEXT` columns (10 of them)** — The bigger gap. Shift4
sends ship-to address data **on the order itself** (`ShipToFirstName`,
`ShipToAddress`, etc.) starting at order placement. Actual
`shift4.shipments` rows do not exist until later, after LPG creates a
shipment in Shift4. An `Order New` webhook therefore has ship-to data
that needs a home but no shipment row to write to.

Alternative approaches considered:
- **Auto-create a placeholder shipment row.** Rejected — would
  require schema changes to `shift4.shipments` (PK can't stay
  `NOT NULL` with no real Shift4 shipment ID yet), and creates
  confusion when a real shipment later arrives.
- **Defer ingestion until a shipment exists.** Rejected — loses
  visibility on new orders for hours or days.
- **Store ship-to on the order itself.** Accepted — matches the
  pattern in the existing `sf_sync.py` integration, mirrors the
  data shape Shift4 actually sends, no PK gymnastics required. The
  `shift4.shipments` table continues to hold per-shipment data once
  shipments are created in Shift4.

Trade-off explicitly accepted: when shipments later materialize,
`shift4.orders.ship_to_*` and `shift4.shipments.ship_*` may diverge
(if the customer or operator edits the address mid-flight). Both
are point-in-time snapshots and that's correct.

## Consequences

**Positive:**

- Webhook handler can be written against a concrete contract derived
  from real LPG data, not docs that gloss over Shift4 quirks.
- Schema additions are small and uncontroversial — text columns plus
  one index. No table restructuring.
- Status filtering at the webhook layer aligns with what the existing
  integration already does — minimizes ingest of statuses we don't
  use, and keeps the schema's CHECK constraint as a safety net.
- Order-level ship-to fields let us ingest order-created webhooks
  cleanly, regardless of whether a `shift4.shipments` row exists yet.

**Negative:**

- Storage and field count on `shift4.orders` grows. Twelve new
  columns is noticeable but not painful.
- Duplication risk between `shift4.orders.ship_to_*` and
  `shift4.shipments.ship_*` if a customer changes address between
  order and shipment. Acceptable as a snapshot pattern; documented
  in architecture.md.

**Reference:**

- Migration: [`migrations/0001_orders_add_invoice_comments_shipto.sql`](../../migrations/0001_orders_add_invoice_comments_shipto.sql)
- Existing integration: `sync/sf_sync.py` in LampPostGlobes_CRM
  (referenced for investigation only; this project does not import
  code from it)
- Related ADRs:
  [ADR-0002](./0002-address-denormalization.md) (address
  denormalization),
  [ADR-0005](./0005-order-items-fk-to-products.md) (FK race
  conditions),
  [ADR-0007](./0007-collapse-account-contact-into-customers.md)
  (single customers table)
