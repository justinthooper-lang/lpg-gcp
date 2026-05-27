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