-- LampPostGlobes GCP database schema
-- Target: Cloud SQL Postgres 16
-- Source of truth: this file. Edit here, then run via psql.
--
-- File structure: schemas → utility function → shift4.* tables in
-- dependency order → lpg.* tables in dependency order. Each table's
-- trigger, indexes, and comments are grouped with it.


-- =============================================================
-- SCHEMAS
-- =============================================================

CREATE SCHEMA IF NOT EXISTS shift4;
CREATE SCHEMA IF NOT EXISTS lpg;

COMMENT ON SCHEMA shift4 IS 'Mirror of data ingested from Shift4Shop. Writes only via webhook.';
COMMENT ON SCHEMA lpg   IS 'Back-office data owned by LampPostGlobes: vendors, POs, invoices, RGAs.';


-- =============================================================
-- UTILITY: auto-update updated_at trigger function
-- =============================================================
-- Lives in the lpg schema (it's an LPG-owned utility) but is attached
-- to triggers on shift4.* tables as well as lpg.* tables.

CREATE OR REPLACE FUNCTION lpg.set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;


-- =============================================================
-- SHIFT4: customers
-- =============================================================
-- One row per Shift4Shop customer record. Person-level data lives here.
-- Registered customers have numeric IDs (e.g. "11875"); guest checkouts
-- get IDs prefixed "guest-" (e.g. "guest-301615") and produce a new
-- customer row each time, even for the same physical person.
--
-- Addresses are NOT stored here — they live on shift4.orders (billing)
-- and shift4.shipments (shipping) for historical accuracy. See ADR-0002.

CREATE TABLE IF NOT EXISTS shift4.customers (
    shift4_customer_id      TEXT            PRIMARY KEY,
    first_name              TEXT,
    last_name               TEXT,
    company_name            TEXT,
    email                   TEXT,
    phone                   TEXT,
    is_guest                BOOLEAN         GENERATED ALWAYS AS (shift4_customer_id LIKE 'guest-%') STORED,
    is_active               BOOLEAN         NOT NULL DEFAULT TRUE,
    raw_payload             JSONB,
    created_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

DROP TRIGGER IF EXISTS trg_customers_updated_at ON shift4.customers;
CREATE TRIGGER trg_customers_updated_at
    BEFORE UPDATE ON shift4.customers
    FOR EACH ROW EXECUTE FUNCTION lpg.set_updated_at();

CREATE INDEX IF NOT EXISTS idx_customers_email ON shift4.customers(email);
CREATE INDEX IF NOT EXISTS idx_customers_guest ON shift4.customers(is_guest) WHERE is_guest = TRUE;

COMMENT ON TABLE shift4.customers IS
    'One row per Shift4 customer record. Guest checkouts produce a new row per checkout (different shift4_customer_id, possibly duplicate email/phone). Mirror faithfully — do not deduplicate at ingest.';


-- =============================================================
-- SHIFT4: orders
-- =============================================================
-- One row per Shift4 order. Quote-status records must NOT be inserted
-- (webhook handler responsibility; DB constraint enforces). All
-- monetary fields are mirrored as Shift4 sends them, for downstream
-- reconciliation against supplier invoices.
--
-- Order statuses observed in Shift4 today (per Shift4Shop UI):
--   New, Processing, Partial, Shipped, Cancel, Hold, Unpaid,
--   Recurring, Review, Quote
-- LPG actively uses: New, Processing, Shipped, Quote. Quote is excluded
-- from ingest. Stored as TEXT, not ENUM, to absorb new values without
-- a migration.

CREATE TABLE IF NOT EXISTS shift4.orders (
    shift4_order_id         BIGINT          PRIMARY KEY,
    shift4_customer_id      TEXT            REFERENCES shift4.customers(shift4_customer_id),

    order_date              TIMESTAMPTZ,
    order_status            TEXT,

    -- Billing address (denormalized per ADR-0002)
    bill_first_name         TEXT,
    bill_last_name          TEXT,
    bill_company            TEXT,
    bill_address            TEXT,
    bill_address2           TEXT,
    bill_city               TEXT,
    bill_state              TEXT,
    bill_zip                TEXT,
    bill_country            TEXT,
    bill_phone              TEXT,
    bill_email              TEXT,

    -- Order-level totals (all mirrored for cost reconciliation)
    subtotal                NUMERIC(12,2),
    tax                     NUMERIC(12,2),
    shipping_cost           NUMERIC(12,2),
    discount                NUMERIC(12,2),
    grand_total             NUMERIC(12,2),

-- Human-readable order identifier (Shift4 InvoiceNumberPrefix + InvoiceNumber)
    invoice_number          TEXT,

    -- Customer-entered comments at checkout
    comments                TEXT,

    -- Ship-to address (denormalized at order-creation time per ADR-0009;
    -- shift4.shipments holds the actual shipment-time address once one
    -- is created in Shift4)
    ship_to_first_name      TEXT,
    ship_to_last_name       TEXT,
    ship_to_company         TEXT,
    ship_to_address         TEXT,
    ship_to_address2        TEXT,
    ship_to_city            TEXT,
    ship_to_state           TEXT,
    ship_to_zip             TEXT,
    ship_to_country         TEXT,
    ship_to_phone           TEXT,

    raw_payload             JSONB,
    created_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    CONSTRAINT chk_orders_status_not_quote
        CHECK (order_status IS DISTINCT FROM 'Quote'),
    CONSTRAINT chk_orders_totals_nonneg CHECK (
        (subtotal      IS NULL OR subtotal      >= 0) AND
        (tax           IS NULL OR tax           >= 0) AND
        (shipping_cost IS NULL OR shipping_cost >= 0) AND
        (discount      IS NULL OR discount      >= 0) AND
        (grand_total   IS NULL OR grand_total   >= 0)
    )
);

DROP TRIGGER IF EXISTS trg_orders_updated_at ON shift4.orders;
CREATE TRIGGER trg_orders_updated_at
    BEFORE UPDATE ON shift4.orders
    FOR EACH ROW EXECUTE FUNCTION lpg.set_updated_at();

CREATE INDEX IF NOT EXISTS idx_orders_customer ON shift4.orders(shift4_customer_id);
CREATE INDEX IF NOT EXISTS idx_orders_date     ON shift4.orders(order_date DESC);
CREATE INDEX IF NOT EXISTS idx_orders_status   ON shift4.orders(order_status);
CREATE INDEX IF NOT EXISTS idx_orders_invoice_number ON shift4.orders(invoice_number);

COMMENT ON TABLE shift4.orders IS
    'Mirror of Shift4 orders. Quote-status orders are filtered at the webhook layer and rejected at the DB layer via CHECK constraint. Billing address denormalized for historical accuracy (ADR-0002).';


-- =============================================================
-- SHIFT4: products
-- =============================================================
-- Customer-facing SKUs as defined in Shift4Shop. May be standalone
-- items or kits; kit composition lives in lpg.product_components.
-- Vendor cost lives on lpg.vendor_skus, NOT here (ADR-0003).

CREATE TABLE IF NOT EXISTS shift4.products (
    sku                     TEXT            PRIMARY KEY,
    name                    TEXT,
    description             TEXT,
    retail_price            NUMERIC(12,2),
    is_active               BOOLEAN         NOT NULL DEFAULT TRUE,
    raw_payload             JSONB,
    created_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_products_price_nonneg CHECK (retail_price IS NULL OR retail_price >= 0)
);

DROP TRIGGER IF EXISTS trg_products_updated_at ON shift4.products;
CREATE TRIGGER trg_products_updated_at
    BEFORE UPDATE ON shift4.products
    FOR EACH ROW EXECUTE FUNCTION lpg.set_updated_at();

COMMENT ON TABLE shift4.products IS
    'Customer-facing SKUs as defined in Shift4Shop. May be standalone items or kits; kit composition lives in lpg.product_components.';


-- =============================================================
-- SHIFT4: shipments
-- =============================================================

CREATE TABLE IF NOT EXISTS shift4.shipments (
    id                      BIGINT          GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    shift4_shipment_id      BIGINT          NOT NULL,   -- Shift4 ShipmentID; 0 at creation, not unique (ADR-0023)
    shift4_order_id         BIGINT          NOT NULL REFERENCES shift4.orders(shift4_order_id),

    -- Shipping address (denormalized per ADR-0002)
    ship_first_name         TEXT,
    ship_last_name          TEXT,
    ship_company            TEXT,
    ship_address            TEXT,
    ship_address2           TEXT,
    ship_city               TEXT,
    ship_state              TEXT,
    ship_zip                TEXT,
    ship_country            TEXT,
    ship_phone              TEXT,
    ship_email              TEXT,

    -- Shipping details
    shipment_method_id      INT,
    shipment_method_name    TEXT,
    customer_shipping_cost  NUMERIC(12,2),
    tracking_code           TEXT,
    shipped_date            TIMESTAMPTZ,

    created_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

DROP TRIGGER IF EXISTS trg_shipments_updated_at ON shift4.shipments;
CREATE TRIGGER trg_shipments_updated_at
    BEFORE UPDATE ON shift4.shipments
    FOR EACH ROW EXECUTE FUNCTION lpg.set_updated_at();

CREATE INDEX IF NOT EXISTS idx_shipments_order ON shift4.shipments(shift4_order_id);


-- =============================================================
-- SHIFT4: order_items
-- =============================================================
-- One row per line item per order. sku has an FK to shift4.products.sku;
-- webhook handler must ensure the product row exists before inserting
-- order_items (see ADR-0005).

CREATE TABLE IF NOT EXISTS shift4.order_items (
    id                      BIGSERIAL       PRIMARY KEY,
    shift4_order_id         BIGINT          NOT NULL REFERENCES shift4.orders(shift4_order_id),
    sku                     TEXT            NOT NULL REFERENCES shift4.products(sku) ON UPDATE CASCADE,
    description             TEXT,
    quantity                INT             NOT NULL,
    unit_price              NUMERIC(12,2)   NOT NULL,
    item_unit_cost_shift4   NUMERIC(12,2),
    line_total              NUMERIC(12,2)   GENERATED ALWAYS AS (quantity * unit_price) STORED,
    created_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_order_items_qty_positive   CHECK (quantity > 0),
    CONSTRAINT chk_order_items_price_nonneg   CHECK (unit_price >= 0)
);

DROP TRIGGER IF EXISTS trg_order_items_updated_at ON shift4.order_items;
CREATE TRIGGER trg_order_items_updated_at
    BEFORE UPDATE ON shift4.order_items
    FOR EACH ROW EXECUTE FUNCTION lpg.set_updated_at();

CREATE INDEX IF NOT EXISTS idx_order_items_order ON shift4.order_items(shift4_order_id);
CREATE INDEX IF NOT EXISTS idx_order_items_sku   ON shift4.order_items(sku);


-- =============================================================
-- LPG: vendors
-- =============================================================

CREATE TABLE IF NOT EXISTS lpg.vendors (
    vendor_id               BIGSERIAL       PRIMARY KEY,
    vendor_code             TEXT            NOT NULL UNIQUE,
    name                    TEXT            NOT NULL,
    po_email                TEXT,
    address_line1           TEXT,
    address_line2           TEXT,
    city                    TEXT,
    state                   TEXT,
    zip                     TEXT,
    country                 TEXT,
    phone                   TEXT,
    min_order_threshold     NUMERIC(12,2),
    min_order_fee           NUMERIC(12,2),
    broken_carton_fee       NUMERIC(12,2),
    notes                   TEXT,
    is_active               BOOLEAN         NOT NULL DEFAULT TRUE,
    created_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_vendors_fees_nonneg CHECK (
        (min_order_threshold IS NULL OR min_order_threshold >= 0) AND
        (min_order_fee       IS NULL OR min_order_fee       >= 0) AND
        (broken_carton_fee   IS NULL OR broken_carton_fee   >= 0)
    )
);

DROP TRIGGER IF EXISTS trg_vendors_updated_at ON lpg.vendors;
CREATE TRIGGER trg_vendors_updated_at
    BEFORE UPDATE ON lpg.vendors
    FOR EACH ROW EXECUTE FUNCTION lpg.set_updated_at();


-- =============================================================
-- LPG: vendor_skus
-- =============================================================

DO $$ BEGIN
    CREATE TYPE lpg.vendor_sku_status AS ENUM ('active', 'discontinued', 'call_for_quote');
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

CREATE TABLE IF NOT EXISTS lpg.vendor_skus (
    vendor_sku_id           BIGSERIAL       PRIMARY KEY,
    vendor_id               BIGINT          NOT NULL REFERENCES lpg.vendors(vendor_id),
    vendor_sku_code         TEXT            NOT NULL,
    description             TEXT,
    unit_cost               NUMERIC(12,2),
    std_pack_qty            INT             NOT NULL DEFAULT 1,
    std_skid_qty            INT,
    status                  lpg.vendor_sku_status NOT NULL DEFAULT 'active',
    notes                   TEXT,
    created_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_vendor_skus_vendor_code UNIQUE (vendor_id, vendor_sku_code),
    CONSTRAINT chk_vendor_skus_cost_nonneg   CHECK (unit_cost     IS NULL OR unit_cost     >= 0),
    CONSTRAINT chk_vendor_skus_pack_positive CHECK (std_pack_qty > 0),
    CONSTRAINT chk_vendor_skus_skid_positive CHECK (std_skid_qty IS NULL OR std_skid_qty > 0)
);

DROP TRIGGER IF EXISTS trg_vendor_skus_updated_at ON lpg.vendor_skus;
CREATE TRIGGER trg_vendor_skus_updated_at
    BEFORE UPDATE ON lpg.vendor_skus
    FOR EACH ROW EXECUTE FUNCTION lpg.set_updated_at();

CREATE INDEX IF NOT EXISTS idx_vendor_skus_vendor ON lpg.vendor_skus(vendor_id);
CREATE INDEX IF NOT EXISTS idx_vendor_skus_status ON lpg.vendor_skus(status) WHERE status != 'active';


-- =============================================================
-- LPG: product_components (bill-of-materials)
-- =============================================================
-- Maps customer-facing SKUs (shift4.products) to vendor SKUs
-- (lpg.vendor_skus) with quantities. See ADR-0004.

CREATE TABLE IF NOT EXISTS lpg.product_components (
    id                      BIGSERIAL       PRIMARY KEY,
    product_sku             TEXT            NOT NULL REFERENCES shift4.products(sku) ON UPDATE CASCADE,
    vendor_sku_id           BIGINT          NOT NULL REFERENCES lpg.vendor_skus(vendor_sku_id),
    quantity                INT             NOT NULL DEFAULT 1,
    sort_order              INT             NOT NULL DEFAULT 1,
    notes                   TEXT,
    created_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_product_components UNIQUE (product_sku, vendor_sku_id),
    CONSTRAINT chk_product_components_qty_positive CHECK (quantity > 0)
);

DROP TRIGGER IF EXISTS trg_product_components_updated_at ON lpg.product_components;
CREATE TRIGGER trg_product_components_updated_at
    BEFORE UPDATE ON lpg.product_components
    FOR EACH ROW EXECUTE FUNCTION lpg.set_updated_at();

CREATE INDEX IF NOT EXISTS idx_product_components_product    ON lpg.product_components(product_sku);
CREATE INDEX IF NOT EXISTS idx_product_components_vendor_sku ON lpg.product_components(vendor_sku_id);

-- ============================================================
-- LPG: vendor_invoices
-- ============================================================
-- One row per Crown invoice PDF ingested from email. Soft-joined to
-- shift4.orders via customer_po_number = shift4.orders.invoice_number
-- (no FK; supports direct-Crown invoices outside our Shift4 PO range).
-- See ADR-0016.

CREATE TABLE IF NOT EXISTS lpg.vendor_invoices (
    vendor_invoice_id       BIGSERIAL PRIMARY KEY,
    vendor_id               BIGINT NOT NULL REFERENCES lpg.vendors(vendor_id),
    vendor_invoice_number   TEXT NOT NULL,
    vendor_order_number     TEXT,
    customer_po_number      TEXT,
    invoice_date            DATE,
    ship_date               DATE,
    ship_via                TEXT,
    tracking_numbers        TEXT[],
    freight_type            TEXT,
    freight_truck           NUMERIC(12,2),
    freight_ups             NUMERIC(12,2),
    subtotal                NUMERIC(12,2),
    sale_amount             NUMERIC(12,2),
    amount_received         NUMERIC(12,2),
    balance_due             NUMERIC(12,2),
    is_replacement          BOOLEAN NOT NULL DEFAULT FALSE,
    raw_pdf_filename        TEXT,
    graph_message_id        TEXT NOT NULL,
    synced_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_vendor_invoices_graph_message_id') THEN
        ALTER TABLE lpg.vendor_invoices ADD CONSTRAINT uq_vendor_invoices_graph_message_id UNIQUE (graph_message_id);
    END IF;
END $$;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_vendor_invoice_number') THEN
        ALTER TABLE lpg.vendor_invoices ADD CONSTRAINT uq_vendor_invoice_number UNIQUE (vendor_id, vendor_invoice_number);
    END IF;
END $$;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_vendor_invoices_freight_type') THEN
        ALTER TABLE lpg.vendor_invoices ADD CONSTRAINT chk_vendor_invoices_freight_type
            CHECK (freight_type IS NULL OR freight_type IN ('ups', 'truck'));
    END IF;
END $$;

DROP TRIGGER IF EXISTS trg_vendor_invoices_updated_at ON lpg.vendor_invoices;
CREATE TRIGGER trg_vendor_invoices_updated_at
    BEFORE UPDATE ON lpg.vendor_invoices
    FOR EACH ROW EXECUTE FUNCTION lpg.set_updated_at();

CREATE INDEX IF NOT EXISTS idx_vendor_invoices_po   ON lpg.vendor_invoices(customer_po_number);
CREATE INDEX IF NOT EXISTS idx_vendor_invoices_date ON lpg.vendor_invoices(invoice_date);

-- ============================================================
-- LPG: vendor_invoice_lines
-- ============================================================
-- One row per L/I (line item) on an invoice. vendor_sku_id is nullable
-- because the PDF may reference a SKU we haven't seeded yet; populated
-- later by re-running scripts/seed_crown_pricing.py. See ADR-0016.

CREATE TABLE IF NOT EXISTS lpg.vendor_invoice_lines (
    vendor_invoice_line_id  BIGSERIAL PRIMARY KEY,
    vendor_invoice_id       BIGINT NOT NULL
                                REFERENCES lpg.vendor_invoices(vendor_invoice_id)
                                ON DELETE CASCADE,
    line_number             INTEGER NOT NULL,
    vendor_sku_code         TEXT NOT NULL,
    vendor_sku_id           BIGINT REFERENCES lpg.vendor_skus(vendor_sku_id),
    qty_shipped             INTEGER NOT NULL,
    qty_backorder           INTEGER NOT NULL DEFAULT 0,
    uom                     TEXT,
    unit_price              NUMERIC(12,4),
    extended_price          NUMERIC(12,2),
    is_fee                  BOOLEAN NOT NULL DEFAULT FALSE,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_invoice_line') THEN
        ALTER TABLE lpg.vendor_invoice_lines ADD CONSTRAINT uq_invoice_line UNIQUE (vendor_invoice_id, line_number);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_invoice_lines_sku
    ON lpg.vendor_invoice_lines (vendor_sku_code);

CREATE INDEX IF NOT EXISTS idx_invoice_lines_is_fee
    ON lpg.vendor_invoice_lines (is_fee)
    WHERE is_fee = TRUE;


-- =============================================================
-- LPG: order field overrides  (ADR-0021)
-- =============================================================
-- LPG-owned corrections that overlay the read-only shift4.orders mirror.
-- shift4.orders stays a faithful mirror of Shift4Shop (source-of-truth rule
-- #1: webhook writes only). When LPG must correct an order field for a
-- back-office workflow (e.g. a missing/garbled ship-to that a PO needs), the
-- corrected value goes here, never into shift4.orders — a re-fired webhook
-- upsert would silently clobber an in-place edit. Reads overlay the two via
-- lpg.v_orders_effective (COALESCE override over mirror).
--
-- Scope: addresses, contact, and comments only. order_status and the monetary
-- totals are deliberately NOT overridable — totals are mirrored expressly to
-- reconcile customer charges against Crown invoices, and a local override would
-- corrupt that reconciliation.
--
-- A NULL override column means "no override" and falls back to the mirror.
-- DELETE the row to revert an order entirely to storefront truth.

CREATE TABLE IF NOT EXISTS lpg.order_overrides (
    shift4_order_id     BIGINT PRIMARY KEY
                            REFERENCES shift4.orders(shift4_order_id)
                            ON DELETE CASCADE,

    -- Billing override (NULL = fall back to shift4.orders)
    bill_first_name     TEXT,
    bill_last_name      TEXT,
    bill_company        TEXT,
    bill_address        TEXT,
    bill_address2       TEXT,
    bill_city           TEXT,
    bill_state          TEXT,
    bill_zip            TEXT,
    bill_country        TEXT,
    bill_phone          TEXT,
    bill_email          TEXT,

    -- Ship-to override (NULL = fall back to shift4.orders)
    ship_to_first_name  TEXT,
    ship_to_last_name   TEXT,
    ship_to_company     TEXT,
    ship_to_address     TEXT,
    ship_to_address2    TEXT,
    ship_to_city        TEXT,
    ship_to_state       TEXT,
    ship_to_zip         TEXT,
    ship_to_country     TEXT,
    ship_to_phone       TEXT,

    -- Comments override
    comments            TEXT,

    -- Provenance — who corrected the order and why (auditability)
    override_reason     TEXT,
    overridden_by       TEXT,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

DROP TRIGGER IF EXISTS trg_order_overrides_updated_at ON lpg.order_overrides;
CREATE TRIGGER trg_order_overrides_updated_at
    BEFORE UPDATE ON lpg.order_overrides
    FOR EACH ROW EXECUTE FUNCTION lpg.set_updated_at();

COMMENT ON TABLE lpg.order_overrides IS
    'LPG-owned corrections overlaying the read-only shift4.orders mirror (ADR-0021). NULL column = no override (falls back to mirror). Addresses/contact/comments only; totals and status are not overridable. Read via lpg.v_orders_effective.';

-- Effective order view: override over mirror. Read paths that need the
-- corrected value (PO builder, admin order detail) read this instead of
-- shift4.orders directly. Non-overridable columns pass through unchanged.
CREATE OR REPLACE VIEW lpg.v_orders_effective AS
SELECT
    o.shift4_order_id,
    o.shift4_customer_id,
    o.order_date,
    o.order_status,

    COALESCE(ov.bill_first_name, o.bill_first_name) AS bill_first_name,
    COALESCE(ov.bill_last_name,  o.bill_last_name)  AS bill_last_name,
    COALESCE(ov.bill_company,    o.bill_company)    AS bill_company,
    COALESCE(ov.bill_address,    o.bill_address)    AS bill_address,
    COALESCE(ov.bill_address2,   o.bill_address2)   AS bill_address2,
    COALESCE(ov.bill_city,       o.bill_city)       AS bill_city,
    COALESCE(ov.bill_state,      o.bill_state)      AS bill_state,
    COALESCE(ov.bill_zip,        o.bill_zip)        AS bill_zip,
    COALESCE(ov.bill_country,    o.bill_country)    AS bill_country,
    COALESCE(ov.bill_phone,      o.bill_phone)      AS bill_phone,
    COALESCE(ov.bill_email,      o.bill_email)      AS bill_email,

    o.subtotal,
    o.tax,
    o.shipping_cost,
    o.discount,
    o.grand_total,

    o.invoice_number,

    COALESCE(ov.comments, o.comments) AS comments,

    COALESCE(ov.ship_to_first_name, o.ship_to_first_name) AS ship_to_first_name,
    COALESCE(ov.ship_to_last_name,  o.ship_to_last_name)  AS ship_to_last_name,
    COALESCE(ov.ship_to_company,    o.ship_to_company)    AS ship_to_company,
    COALESCE(ov.ship_to_address,    o.ship_to_address)    AS ship_to_address,
    COALESCE(ov.ship_to_address2,   o.ship_to_address2)   AS ship_to_address2,
    COALESCE(ov.ship_to_city,       o.ship_to_city)       AS ship_to_city,
    COALESCE(ov.ship_to_state,      o.ship_to_state)      AS ship_to_state,
    COALESCE(ov.ship_to_zip,        o.ship_to_zip)        AS ship_to_zip,
    COALESCE(ov.ship_to_country,    o.ship_to_country)    AS ship_to_country,
    COALESCE(ov.ship_to_phone,      o.ship_to_phone)      AS ship_to_phone,

    o.raw_payload,
    o.created_at,
    o.updated_at,

    (ov.shift4_order_id IS NOT NULL) AS has_override
FROM shift4.orders o
LEFT JOIN lpg.order_overrides ov USING (shift4_order_id);

COMMENT ON VIEW lpg.v_orders_effective IS
    'shift4.orders with lpg.order_overrides applied (COALESCE override over mirror). has_override flags whether a correction row exists. Source-of-truth-preserving read surface (ADR-0021).';
