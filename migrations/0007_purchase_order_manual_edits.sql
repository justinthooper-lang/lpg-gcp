-- migrations/0007_purchase_order_manual_edits.sql
-- ADR-0022: editable draft-PO lines.
--
-- Adds a flag marking a PO whose lines have been added/edited/deleted by hand in
-- the admin UI. Regeneration-from-order (generate_purchase_order) DELETEs and
-- rebuilds all lines from the storefront order; without this flag a stray
-- "Generate" click would silently discard manual edits. With it, regeneration
-- refuses to overwrite a hand-edited PO unless explicitly forced, and resets the
-- flag to false when a forced regeneration does replace the lines.
--
-- (The PO tables themselves live in migration 0004, not schema.sql; this column
-- follows that same migration-only home.)
--
-- Idempotent: ADD COLUMN IF NOT EXISTS.

ALTER TABLE lpg.purchase_orders
    ADD COLUMN IF NOT EXISTS manually_edited boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN lpg.purchase_orders.manually_edited IS
    'True once a PO''s lines have been added/edited/deleted by hand in the admin UI (ADR-0022). Regeneration from the order refuses to overwrite such a PO unless explicitly forced (?force=true), and resets this flag to false when a forced regeneration replaces the lines.';
