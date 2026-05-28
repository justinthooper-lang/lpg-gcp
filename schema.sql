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
    shift4_shipment_id      BIGINT          PRIMARY KEY,
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
    shift4_shipment_id      BIGINT          REFERENCES shift4.shipments(shift4_shipment_id),
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
