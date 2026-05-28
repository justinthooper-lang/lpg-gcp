# Architecture

This document describes how the LPG GCP rebuild is structured and the
rules that keep it coherent. For the **why** behind specific choices,
see [decisions/](./decisions/).

## The big picture

LPG has two distinct kinds of data:

1. **Storefront data** — what Shift4Shop knows about. Customers, orders,
   shipments, line items, the catalog. Shift4 is the system of record;
   we mirror it locally so we can query it without hammering their API.
2. **Back-office data** — what only LPG knows about. Vendors, vendor
   SKUs, purchase orders, invoices, returns, internal cost structures,
   kit compositions. Shift4 has no concept of any of this.

These map to two Postgres schemas:

| Schema | Owner | Write path | Source of truth |
|---|---|---|---|
| `shift4` | Shift4Shop | Webhook ingest only | Shift4Shop |
| `lpg` | LPG | Internal apps + manual entry | This database |

The split is the single most important architectural rule in the project.
Everything else follows from it.

## Source-of-truth rules

These are non-negotiable. Violating them creates the kind of bugs that
take days to find.

1. **`shift4.*` is read-mostly and append-friendly.** Webhook handlers
   write. Nothing else writes. If you find yourself wanting to UPDATE a
   `shift4.*` row from an internal app, you're doing it wrong — that
   data belongs in `lpg.*`.
2. **`lpg.*` never references storefront-internal facts.** Don't put a
   `retail_price` column on `lpg.products` (it doesn't exist as a table
   anyway — see below). Don't duplicate customer addresses into `lpg.*`.
   If you need storefront data in a back-office workflow, JOIN to
   `shift4.*` at read time.
3. **Vendor cost lives on `lpg.vendor_skus.unit_cost`, never on a
   product.** Products are what customers buy. Vendor SKUs are what LPG
   purchases. They are not the same thing, even when they look the same.
   See [ADR-0003](./decisions/0003-vendor-cost-in-vendor-skus.md).
4. **Customer-facing SKUs map to vendor SKUs via the BOM table**
   (`lpg.product_components`), not via 1:1 columns. A "globe" sold to a
   customer might be assembled from a globe body, a finial, and a mounting
   kit from three different vendors. See
   [ADR-0004](./decisions/0004-bom-via-product-components.md).
5. **One `shift4.customers` table; no separate companies table.** The
   Account/Contact split that exists in the current Salesforce CRM is
   not reproduced here — it would be 50% junk rows given how the data
   actually looks. See
   [ADR-0007](./decisions/0007-collapse-account-contact-into-customers.md).

## Schema inventory

### `shift4` schema (mirror of Shift4Shop)

#### `shift4.customers`

One row per Shift4 customer record. Person-level data lives here
(`first_name`, `last_name`, `email`, `phone`), plus `company_name`
which stores whatever the customer typed at checkout (a real company
name for B2B, or the person's own name for B2C — we don't try to tell
them apart).

The PK `shift4_customer_id` is `TEXT`, not `BIGINT` — Shift4 prefixes
guest checkout IDs with `guest-` (e.g. `guest-301615`), while registered
customers get plain numeric IDs (e.g. `11875`). The `is_guest` column
is a generated stored column derived from `shift4_customer_id LIKE 'guest-%'`,
so it can never get out of sync with the ID.

Addresses are **not** on this table — they live on `shift4.orders`
(billing) and `shift4.shipments` (shipping) for historical accuracy.
See [ADR-0002](./decisions/0002-address-denormalization.md).

#### `shift4.orders`

One row per Shift4 order. The PK `shift4_order_id` is `BIGINT` (Shift4's
order IDs are numeric, unlike customer IDs).

Billing address fields live directly on the row, denormalized per
[ADR-0002](./decisions/0002-address-denormalization.md). All five
order-level monetary fields are mirrored — `subtotal`, `tax`,
`shipping_cost`, `discount`, `grand_total` — because LPG needs them
for reconciling customer charges against supplier invoices.

`order_status` is stored as `TEXT` (not an ENUM) so new Shift4 status
values can be absorbed without a schema migration. Shift4 currently
exposes 11 statuses (New, Processing, Partial, Shipped, Cancel, Hold,
Unpaid, Recurring, Review, Quote); LPG actively uses 4 (New, Processing,
Shipped, Quote).

**Quote-status orders must not be ingested.** This is webhook handler
logic — Quote-status records are filtered before insert. As a safety
net, the database also enforces this via the `chk_orders_status_not_quote`
CHECK constraint. If the webhook handler ever has a bug, the database
catches it.

Payment data is intentionally not mirrored. That stays in Shift4.

#### `shift4.shipments`

One row per shipment within an order. Shipping address is denormalized
on the row per [ADR-0002](./decisions/0002-address-denormalization.md).
Tracking code, shipping method, and cost-to-customer are stored.

#### `shift4.order_items`

One row per line item per order. FK to `shift4.products.sku` and to
`shift4.orders.shift4_order_id`. `line_total` is a generated column
(`quantity * unit_price`). `item_unit_cost_shift4` mirrors whatever
Shift4 reports as the cost — useful for the supplier-invoice
reconciliation work.

#### `shift4.products`

One row per customer-facing SKU. Webhook-fed mirror of the Shift4
catalog. `raw_payload` JSONB holds the full Shift4 payload for forensics
when something doesn't reconcile.

### `lpg` schema (back-office, LPG-owned)

| Table | Purpose |
|---|---|
| `vendors` | One row per supplier. Includes minimum-order thresholds and broken-carton fees because both affect cost-of-goods calculations. |
| `vendor_skus` | One row per SKU LPG can purchase from a vendor. Includes `unit_cost`, standard pack quantity, skid quantity, and a status enum (active / discontinued / call_for_quote). |
| `product_components` | The bill-of-materials. Maps `shift4.products.sku` to one or more `lpg.vendor_skus` with a quantity. This is how kits are represented. |

### Conventions

- All tables have `created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()`.
- All tables have `updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()`,
  maintained by a trigger calling `lpg.set_updated_at()`. The trigger
  function lives in the `lpg` schema deliberately — it's an LPG-owned
  utility, even when it's attached to `shift4.*` tables.
- Money columns are `NUMERIC(12,2)`. Never `FLOAT` or `DOUBLE`. Floating
  point is for science, not invoices.
- IDs that come from Shift4 are stored with the `shift4_` prefix
  (e.g. `shift4_order_id`, `shift4_customer_id`). IDs that LPG generates
  use `BIGSERIAL`. This makes the origin of any ID obvious from its name.
- CHECK constraints guard against impossible data (negative prices,
  zero quantities, Quote-status orders). Cheap insurance against bad
  webhook payloads or app-layer bugs.
- Generated stored columns are used where a derived value should be
  guaranteed in sync with its source (e.g. `is_guest`, `line_total`).

## Data flow

### Inbound (Shift4 → us)

```
Shift4Shop event ──> Webhook endpoint ──> Pub/Sub topic ──> Subscriber
                       (Cloud Run)         (decouple)        (Cloud Run)
                                                                │
                                                                ▼
                                                          shift4.* tables
                                                          (Cloud SQL)
```

Not built yet. Today the schema exists but nothing populates it. The
Pub/Sub interposition is intentional: it lets the webhook endpoint
respond fast (Shift4 expects a quick 2xx), and lets the subscriber
retry on failure without re-receiving from Shift4.

### Outbound (us → vendors, customers)

Planned, not designed yet. Likely flows:
- PO generation → email to vendor (`lpg.vendors.po_email`)
- RGA → email / printable PDF for customer

## Database deployments

The schema runs in two places: a local Postgres for offline iteration
and Cloud SQL for the real GCP-hosted instance. Both apply the same
`schema.sql`.

### Cloud SQL (preferred for normal dev work)

**Instance:** `lpg-dev` in `us-west1` (Oregon)
**Tier:** `db-f1-micro` (shared-core, ~$10/mo at 24/7)
**Edition:** Enterprise
**Version:** Postgres 16.13
**Connection:** via Cloud SQL Auth Proxy
**Default state:** STOPPED between sessions to save cost
**Provisioning rationale:** [ADR-0008](./decisions/0008-cloud-sql-provisioning.md)

**Start of session:**
```bash
# 1. Start the instance (takes ~1-2 min to become RUNNABLE)
gcloud sql instances patch lpg-dev --activation-policy=ALWAYS

# 2. In a dedicated terminal tab, run the Auth Proxy. Keep it open.
cloud-sql-proxy lpg-dev-496820:us-west1:lpg-dev

# 3. In your main tab, connect.
psql -h 127.0.0.1 -U postgres -d lpg
```

**End of session:**
```bash
# 1. Exit psql
\q

# 2. Ctrl+C the Auth Proxy in its tab

# 3. Stop the instance — drops billing to storage-only (~$0.30/day)
gcloud sql instances patch lpg-dev --activation-policy=NEVER
```

**Important details:**
- The `postgres` user on Cloud SQL is **near-superuser, not full
  superuser.** Google reserves the actual superuser role for managing
  the instance. You can create databases, schemas, roles, tables, and
  install extensions from an approved list. You cannot bypass certain
  security policies or alter system catalogs. Note the `lpg=>` (not
  `lpg=#`) prompt in psql — that's how you can tell.
- Password is stored in 1Password. If lost, reset with
  `gcloud sql users set-password postgres --instance=lpg-dev --prompt-for-password`.
- Always stop the instance between sessions. Forgetting costs $0.30/day
  in compute on top of storage.

### Local Postgres (offline iteration)

```bash
brew services start postgresql@16
psql -d lpg
# work
brew services stop postgresql@16   # frees port 5432 if you want to use the proxy
```

Local install uses `trust` auth (no password) for local connections —
fine for development. Verified locally on 2026-05-28 (8 tables,
triggers, indexes, constraints all apply cleanly).

**Conflict warning:** local Postgres and Cloud SQL Auth Proxy both
default to listening on port 5432. Run one or the other at a time,
or pass `--port 5433` to the proxy.

## Known issues / open questions

These are not solved problems. Listed here so they're visible.

1. **Webhook ordering race condition.** Two FK relationships have the
   same race: `shift4.order_items.sku → shift4.products.sku` and
   `shift4.orders.shift4_customer_id → shift4.customers.shift4_customer_id`.
   If a dependent webhook arrives before its parent, the insert fails.
   See [ADR-0005](./decisions/0005-order-items-fk-to-products.md) for
   the current thinking. Resolution TBD — will be decided when the
   webhook handler is built.
2. **Guest checkout duplication.** The same physical person buying
   three times as a guest produces three separate customer rows with
   different `shift4_customer_id` values. This is faithful to Shift4
   and intentional — deduplication, if needed, is a downstream concern,
   not an ingestion concern.
3. **No webhook handler yet.** Schema applies to both local and Cloud
   SQL, but nothing populates the tables. The Cloud Run service that
   ingests Shift4 webhooks is the next major build.
4. **Password auth, not IAM auth.** Cloud SQL supports IAM database
   authentication, which would eliminate the postgres password
   handling entirely. We chose password for setup simplicity; switching
   to IAM is a future ADR when we add additional users.
