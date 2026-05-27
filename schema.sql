-- LampPostGlobes GCP database schema
-- Target: Cloud SQL Postgres 16
-- Source of truth: this file. Edit here, then run via psql.

-- =============================================================
-- SCHEMAS
-- =============================================================

CREATE SCHEMA IF NOT EXISTS shift4;
CREATE SCHEMA IF NOT EXISTS lpg;

COMMENT ON SCHEMA shift4 IS 'Mirror of data ingested from Shift4Shop. Writes only via webhook.';
COMMENT ON SCHEMA lpg   IS 'Back-office data owned by LampPostGlobes: vendors, POs, invoices, RGAs.';
CREATE TABLE IF NOT EXISTS shift4.shipments (
    shift4_shipment_id      BIGINT          PRIMARY KEY,
    shift4_order_id         BIGINT          NOT NULL REFERENCES shift4.orders(shift4_order_id),

    -- Shipping address (separate from billing)
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

    created_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS shift4.order_items (
    id                      BIGSERIAL       PRIMARY KEY,
    shift4_order_id         BIGINT          NOT NULL REFERENCES shift4.orders(shift4_order_id),
    shift4_shipment_id      BIGINT          REFERENCES shift4.shipments(shift4_shipment_id),
    sku                     TEXT            NOT NULL,
    description             TEXT,
    quantity                INT             NOT NULL,
    unit_price              NUMERIC(12,2)   NOT NULL,
    item_unit_cost_shift4   NUMERIC(12,2),
    line_total              NUMERIC(12,2)   GENERATED ALWAYS AS (quantity * unit_price) STORED
);

-- Indexes for common lookups
CREATE INDEX IF NOT EXISTS idx_orders_customer    ON shift4.orders(shift4_customer_id);
CREATE INDEX IF NOT EXISTS idx_orders_date        ON shift4.orders(order_date DESC);
CREATE INDEX IF NOT EXISTS idx_shipments_order    ON shift4.shipments(shift4_order_id);
CREATE INDEX IF NOT EXISTS idx_order_items_order  ON shift4.order_items(shift4_order_id);
CREATE INDEX IF NOT EXISTS idx_order_items_sku    ON shift4.order_items(sku);
-- =============================================================
-- TRIGGER FUNCTION: auto-update updated_at on UPDATE
-- =============================================================

CREATE OR REPLACE FUNCTION lpg.set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_customers_updated_at ON shift4.customers;
CREATE TRIGGER trg_customers_updated_at
    BEFORE UPDATE ON shift4.customers
    FOR EACH ROW EXECUTE FUNCTION lpg.set_updated_at();

DROP TRIGGER IF EXISTS trg_orders_updated_at ON shift4.orders;
CREATE TRIGGER trg_orders_updated_at
    BEFORE UPDATE ON shift4.orders
    FOR EACH ROW EXECUTE FUNCTION lpg.set_updated_at();

ALTER TABLE shift4.shipments
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

ALTER TABLE shift4.order_items
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

DROP TRIGGER IF EXISTS trg_shipments_updated_at ON shift4.shipments;
CREATE TRIGGER trg_shipments_updated_at
    BEFORE UPDATE ON shift4.shipments
    FOR EACH ROW EXECUTE FUNCTION lpg.set_updated_at();

DROP TRIGGER IF EXISTS trg_order_items_updated_at ON shift4.order_items;
CREATE TRIGGER trg_order_items_updated_at
    BEFORE UPDATE ON shift4.order_items
    FOR EACH ROW EXECUTE FUNCTION lpg.set_updated_at();

ALTER TABLE shift4.order_items
    ADD CONSTRAINT chk_order_items_qty_positive CHECK (quantity > 0),
    ADD CONSTRAINT chk_order_items_price_nonneg CHECK (unit_price >= 0);


-- =============================================================
-- SHIFT4: products
-- =============================================================

CREATE TABLE IF NOT EXISTS shift4.products (
    sku                     TEXT            PRIMARY KEY,
    name                    TEXT,
    description             TEXT,
    retail_price            NUMERIC(12,2),
    is_active               BOOLEAN         NOT NULL DEFAULT TRUE,
    created_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    raw_payload             JSONB,
    CONSTRAINT chk_products_price_nonneg CHECK (retail_price IS NULL OR retail_price >= 0)
);

DROP TRIGGER IF EXISTS trg_products_updated_at ON shift4.products;
CREATE TRIGGER trg_products_updated_at
    BEFORE UPDATE ON shift4.products
    FOR EACH ROW EXECUTE FUNCTION lpg.set_updated_at();

COMMENT ON TABLE shift4.products IS
    'Customer-facing SKUs as defined in Shift4Shop. May be standalone items or kits; kit composition lives in lpg.product_components.';


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
    CONSTRAINT chk_vendor_skus_cost_nonneg CHECK (unit_cost IS NULL OR unit_cost >= 0),
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
-- LPG: product_components
-- =============================================================

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

CREATE INDEX IF NOT EXISTS idx_product_components_product ON lpg.product_components(product_sku);
CREATE INDEX IF NOT EXISTS idx_product_components_vendor_sku ON lpg.product_components(vendor_sku_id);


-- =============================================================
-- FK retrofit: shift4.order_items.sku -> shift4.products.sku
-- =============================================================

DO $$ BEGIN
    ALTER TABLE shift4.order_items
        ADD CONSTRAINT fk_order_items_product
        FOREIGN KEY (sku) REFERENCES shift4.products(sku) ON UPDATE CASCADE;
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;