-- Migration 0002: Add lpg.vendor_invoices and lpg.vendor_invoice_lines
-- See ADR-0016 for design rationale.
--
-- Two new tables for ingesting vendor (Crown) invoices from email:
--   lpg.vendor_invoices      — one row per Crown invoice PDF
--   lpg.vendor_invoice_lines — one row per L/I (line item) on each invoice
--
-- Soft-joined to shift4.orders via customer_po_number = invoice_number
-- (no FK; supports direct-Crown invoices outside our Shift4 PO range).
--
-- Apply locally via:
--   psql -h 127.0.0.1 -U postgres -d lpg -f migrations/0002_add_vendor_invoices.sql
--
-- Idempotent: pre-checks pg_constraint / pg_trigger before adding.

BEGIN;

-- ---------------------------------------------------------------
-- lpg.vendor_invoices
-- ---------------------------------------------------------------

CREATE TABLE IF NOT EXISTS lpg.vendor_invoices (
    vendor_invoice_id       BIGSERIAL PRIMARY KEY,
    vendor_id               BIGINT NOT NULL REFERENCES lpg.vendors(vendor_id),

    -- Identifiers from the PDF
    vendor_invoice_number   TEXT NOT NULL,
    vendor_order_number     TEXT,
    customer_po_number      TEXT,

    -- Dates
    invoice_date            DATE,
    ship_date               DATE,

    -- Shipping
    ship_via                TEXT,
    tracking_numbers        TEXT[],
    freight_type            TEXT,
    freight_truck           NUMERIC(12,2),
    freight_ups             NUMERIC(12,2),

    -- Money (verbatim from PDF totals block)
    subtotal                NUMERIC(12,2),
    sale_amount             NUMERIC(12,2),
    amount_received         NUMERIC(12,2),
    balance_due             NUMERIC(12,2),

    -- Status / classification
    is_replacement          BOOLEAN NOT NULL DEFAULT FALSE,
    raw_pdf_filename        TEXT,

    -- Sync provenance / idempotency
    graph_message_id        TEXT NOT NULL,
    synced_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'uq_vendor_invoices_graph_message_id'
    ) THEN
        ALTER TABLE lpg.vendor_invoices
            ADD CONSTRAINT uq_vendor_invoices_graph_message_id
            UNIQUE (graph_message_id);
    END IF;
END $$;

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'uq_vendor_invoice_number'
    ) THEN
        ALTER TABLE lpg.vendor_invoices
            ADD CONSTRAINT uq_vendor_invoice_number
            UNIQUE (vendor_id, vendor_invoice_number);
    END IF;
END $$;

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'chk_vendor_invoices_freight_type'
    ) THEN
        ALTER TABLE lpg.vendor_invoices
            ADD CONSTRAINT chk_vendor_invoices_freight_type
            CHECK (freight_type IS NULL OR freight_type IN ('ups', 'truck'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_vendor_invoices_po
    ON lpg.vendor_invoices (customer_po_number);

CREATE INDEX IF NOT EXISTS idx_vendor_invoices_date
    ON lpg.vendor_invoices (invoice_date);

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'trg_vendor_invoices_updated_at'
    ) THEN
        CREATE TRIGGER trg_vendor_invoices_updated_at
            BEFORE UPDATE ON lpg.vendor_invoices
            FOR EACH ROW EXECUTE FUNCTION lpg.set_updated_at();
    END IF;
END $$;

-- ---------------------------------------------------------------
-- lpg.vendor_invoice_lines
-- ---------------------------------------------------------------

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

    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'uq_invoice_line'
    ) THEN
        ALTER TABLE lpg.vendor_invoice_lines
            ADD CONSTRAINT uq_invoice_line
            UNIQUE (vendor_invoice_id, line_number);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_invoice_lines_sku
    ON lpg.vendor_invoice_lines (vendor_sku_code);

COMMIT;