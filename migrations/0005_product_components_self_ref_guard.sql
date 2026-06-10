-- migrations/0005_product_components_self_ref_guard.sql
-- Enforce the ADR-0018 explosion invariant at the database level.
--
-- Invariant: a lpg.product_components row may exist ONLY when a storefront SKU
-- decomposes into DIFFERENT vendor components. A self-referential row — one whose
-- product_sku equals the vendor_sku_code of the component it points at — represents
-- a passthrough, and under the invariant a passthrough is the ABSENCE of a row.
-- Such rows must therefore be unstorable.
--
-- Two parts:
--   1. Idempotent cleanup of any existing self-referential rows. This records, as a
--      repo artifact, the manual live-DB cleanup performed 2026-06-10 (two rows:
--      20012-CL-4F and 20014-WH-6F, each mapping to itself). On a clean DB this
--      deletes nothing.
--   2. A BEFORE INSERT OR UPDATE trigger that rejects self-referential rows going
--      forward. A trigger (not a CHECK constraint) is required because the component
--      code lives in a different table (lpg.vendor_skus), so the comparison is
--      cross-table and a CHECK cannot express it.
--
-- Idempotent: safe to re-run via \i (CREATE OR REPLACE FUNCTION, DROP TRIGGER IF
-- EXISTS + CREATE, and a DELETE that no-ops when nothing matches).

-- ---------------------------------------------------------------------------
-- 1. Clean up any existing self-referential rows.
-- ---------------------------------------------------------------------------
DELETE FROM lpg.product_components pc
USING lpg.vendor_skus vs
WHERE pc.vendor_sku_id = vs.vendor_sku_id
  AND pc.product_sku = vs.vendor_sku_code;

-- ---------------------------------------------------------------------------
-- 2. Guard: reject self-referential rows on write.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION lpg.reject_self_referential_component()
RETURNS trigger AS $$
DECLARE
    component_code text;
BEGIN
    SELECT vendor_sku_code
      INTO component_code
      FROM lpg.vendor_skus
     WHERE vendor_sku_id = NEW.vendor_sku_id;

    IF component_code = NEW.product_sku THEN
        RAISE EXCEPTION
            'Self-referential product_components row rejected: product_sku "%" maps to its own vendor SKU (vendor_sku_id %). A passthrough SKU must NOT have a product_components row — under the ADR-0018 explosion invariant, a row exists only when a SKU decomposes into different components.',
            NEW.product_sku, NEW.vendor_sku_id;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_product_components_reject_self_ref
    ON lpg.product_components;
CREATE TRIGGER trg_product_components_reject_self_ref
    BEFORE INSERT OR UPDATE ON lpg.product_components
    FOR EACH ROW
    EXECUTE FUNCTION lpg.reject_self_referential_component();
