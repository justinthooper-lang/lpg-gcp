-- Migration 0003: Add is_fee flag to lpg.vendor_invoice_lines
-- See ADR-0016 (invoice ingest) for design rationale.
--
-- Crown invoice line items mix real product SKUs with fee pseudo-SKUs
-- (e.g. "MIN ORDER FEE", "BKN CTN FEE"). The parser already classifies
-- each line; this column persists that classification so the profit
-- math can split parts cost from fees with a clean boolean rather than
-- string-matching vendor_sku_code at query time.
--
-- Fees carry vendor_sku_id = NULL (no real SKU to reference).
--
-- Apply locally via:
--   psql -h 127.0.0.1 -U postgres -d lpg -f migrations/0003_vendor_invoice_lines_add_is_fee.sql
--
-- Idempotent: ADD COLUMN IF NOT EXISTS. NOT NULL is safe on existing
-- rows because of the FALSE default (existing lines treated as non-fee).

BEGIN;

ALTER TABLE lpg.vendor_invoice_lines
    ADD COLUMN IF NOT EXISTS is_fee BOOLEAN NOT NULL DEFAULT FALSE;

-- Partial index: queries that isolate fee lines (profit breakdown,
-- "which orders incurred fees") hit only the small fee subset.
CREATE INDEX IF NOT EXISTS idx_invoice_lines_is_fee
    ON lpg.vendor_invoice_lines (is_fee)
    WHERE is_fee = TRUE;

COMMIT;