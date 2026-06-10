-- migrations/0004_purchase_orders.sql
-- Purchase order generation (ADR-0018).
-- Header (one PO per Shift4 order) + lines. Lines are snapshots of what was
-- printed on the PO sent to Crown: product lines carry vendor_sku_code/qty/unit_cost
-- (with a nullable vendor_sku_id FK for integrity where the SKU exists), fee lines
-- carry a label (description) + amount, is_fee = true.
--
-- Idempotent: safe to re-run via \i. Non-idempotent DDL (CREATE TYPE, ADD CONSTRAINT)
-- is guarded with DO $$ ... EXCEPTION WHEN duplicate_object THEN NULL; END $$.

-- ---------------------------------------------------------------------------
-- Enum: purchase order status
-- ---------------------------------------------------------------------------
DO $$ BEGIN
    CREATE TYPE lpg.purchase_order_status AS ENUM ('draft', 'sent');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ---------------------------------------------------------------------------
-- Header: lpg.purchase_orders
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS lpg.purchase_orders (
    purchase_order_id   bigserial PRIMARY KEY,

    -- PO number = the Shift4 *invoice number* (shift4.orders.invoice_number,
    -- e.g. 'PO32163') — the human ID that appears on Crown invoices and is the
    -- three-way-match join key (ADR-0009). NOT the internal shift4_order_id.
    -- One PO per order.
    po_number           text   NOT NULL,
    shift4_order_id     bigint NOT NULL,
    vendor_id           bigint NOT NULL,

    status              lpg.purchase_order_status NOT NULL DEFAULT 'draft',

    -- Ship-to snapshot (dropship: the end customer, frozen at generation time).
    ship_name           text,
    ship_company        text,
    ship_street         text,
    ship_city_line      text,
    ship_phone          text,

    comments            text,

    -- Filled once the PDF is rendered/stored (Q5).
    pdf_gcs_uri         text,
    sent_at             timestamptz,

    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

-- One PO per Shift4 order: regeneration updates in place rather than minting a dup.
DO $$ BEGIN
    ALTER TABLE lpg.purchase_orders
        ADD CONSTRAINT uq_purchase_orders_po_number UNIQUE (po_number);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE lpg.purchase_orders
        ADD CONSTRAINT purchase_orders_shift4_order_id_fkey
        FOREIGN KEY (shift4_order_id) REFERENCES shift4.orders (shift4_order_id);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE lpg.purchase_orders
        ADD CONSTRAINT purchase_orders_vendor_id_fkey
        FOREIGN KEY (vendor_id) REFERENCES lpg.vendors (vendor_id);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

CREATE INDEX IF NOT EXISTS idx_purchase_orders_order
    ON lpg.purchase_orders (shift4_order_id);
CREATE INDEX IF NOT EXISTS idx_purchase_orders_vendor
    ON lpg.purchase_orders (vendor_id);

-- ---------------------------------------------------------------------------
-- Lines: lpg.purchase_order_lines
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS lpg.purchase_order_lines (
    purchase_order_line_id  bigserial PRIMARY KEY,
    purchase_order_id       bigint  NOT NULL,

    is_fee                  boolean NOT NULL DEFAULT false,

    -- Product-line snapshot (null on fee lines). vendor_sku_id is a nullable FK:
    -- present where the SKU exists in vendor_skus, null for passthrough SKUs that
    -- have no row. vendor_sku_code/description/unit_cost are the printed snapshot.
    vendor_sku_id           bigint,
    vendor_sku_code         text,
    description             text,
    quantity               integer,
    unit_cost               numeric(12,2),

    -- Fee-line amount (null on product lines). e.g. description='Order Fee', amount=15.00.
    amount                  numeric(12,2),

    sort_order              integer NOT NULL DEFAULT 1,

    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now()
);

DO $$ BEGIN
    ALTER TABLE lpg.purchase_order_lines
        ADD CONSTRAINT purchase_order_lines_purchase_order_id_fkey
        FOREIGN KEY (purchase_order_id)
        REFERENCES lpg.purchase_orders (purchase_order_id) ON DELETE CASCADE;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE lpg.purchase_order_lines
        ADD CONSTRAINT purchase_order_lines_vendor_sku_id_fkey
        FOREIGN KEY (vendor_sku_id) REFERENCES lpg.vendor_skus (vendor_sku_id);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- Shape guard: a fee line carries an amount and no product fields; a product line
-- carries product fields and no amount. Keeps the two kinds honest at the DB level.
DO $$ BEGIN
    ALTER TABLE lpg.purchase_order_lines
        ADD CONSTRAINT chk_purchase_order_lines_kind CHECK (
            (is_fee = true
                AND amount IS NOT NULL
                AND vendor_sku_id IS NULL
                AND vendor_sku_code IS NULL
                AND quantity IS NULL
                AND unit_cost IS NULL)
            OR
            (is_fee = false
                AND amount IS NULL
                AND quantity IS NOT NULL
                AND unit_cost IS NOT NULL)
        );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE lpg.purchase_order_lines
        ADD CONSTRAINT chk_purchase_order_lines_qty_positive
        CHECK (quantity IS NULL OR quantity > 0);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE lpg.purchase_order_lines
        ADD CONSTRAINT chk_purchase_order_lines_cost_nonneg
        CHECK (unit_cost IS NULL OR unit_cost >= 0);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE lpg.purchase_order_lines
        ADD CONSTRAINT chk_purchase_order_lines_amount_nonneg
        CHECK (amount IS NULL OR amount >= 0);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

CREATE INDEX IF NOT EXISTS idx_purchase_order_lines_po
    ON lpg.purchase_order_lines (purchase_order_id);

-- ---------------------------------------------------------------------------
-- updated_at triggers (lpg.set_updated_at, wired like every other table)
-- ---------------------------------------------------------------------------
DROP TRIGGER IF EXISTS trg_purchase_orders_updated_at ON lpg.purchase_orders;
CREATE TRIGGER trg_purchase_orders_updated_at
    BEFORE UPDATE ON lpg.purchase_orders
    FOR EACH ROW EXECUTE FUNCTION lpg.set_updated_at();

DROP TRIGGER IF EXISTS trg_purchase_order_lines_updated_at ON lpg.purchase_order_lines;
CREATE TRIGGER trg_purchase_order_lines_updated_at
    BEFORE UPDATE ON lpg.purchase_order_lines
    FOR EACH ROW EXECUTE FUNCTION lpg.set_updated_at();
