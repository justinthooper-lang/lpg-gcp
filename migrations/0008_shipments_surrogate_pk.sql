-- migrations/0008_shipments_surrogate_pk.sql
-- ADR-0023: re-key shift4.shipments with a surrogate primary key.
--
-- Bug: shift4.shipments used shift4_shipment_id as a single-column PRIMARY KEY,
-- but Shift4 sends ShipmentID=0 for every order at creation time (a real id is
-- only assigned later, when a shipment is actually created). The first order's
-- shipment inserts id=0 fine; every subsequent order collides on the PK (SQLSTATE
-- 23505, "duplicate key value violates unique constraint shipments_pkey"). Because
-- the whole order ingests in one transaction, the collision rolls the entire order
-- back -- so every real order after the first was silently dropped, while the
-- webhook returned 500 and Shift4 retried in vain.
--
-- Fix: give shift4.shipments a surrogate identity primary key and demote
-- shift4_shipment_id to a plain (non-unique) data column. The ingest already does
-- DELETE FROM shift4.shipments WHERE shift4_order_id = %s before re-inserting, so
-- shift4_order_id remains the true idempotency key; the surrogate id just gives
-- each row a unique identity. shift4_shipment_id keeps Shift4's value (0 at
-- creation, a real id later on re-ingest).
--
-- Also drops shift4.order_items.shift4_shipment_id: a vestigial FK column that
-- depended on the old shipments PK, was never populated by the ingest, and is
-- never read. Dropping it removes the dependency that would otherwise block the
-- PK change.
--
-- No code change accompanies this: the running handler already omits
-- shift4_shipment_id from its order_items INSERT and inserts shift4_shipment_id
-- into shipments as a plain value, so it becomes correct the moment the unique
-- constraint is gone. Migration-only; no redeploy required.
--
-- Idempotent: IF EXISTS / IF NOT EXISTS guards and a PK-existence check.

-- 1. Drop the unused FK column on order_items (depended on the old shipments PK).
ALTER TABLE shift4.order_items
    DROP COLUMN IF EXISTS shift4_shipment_id;

-- 2. Drop the old single-column primary key on shift4_shipment_id.
ALTER TABLE shift4.shipments
    DROP CONSTRAINT IF EXISTS shipments_pkey;

-- 3. Add a surrogate identity column. Existing rows backfill automatically.
ALTER TABLE shift4.shipments
    ADD COLUMN IF NOT EXISTS id BIGINT GENERATED ALWAYS AS IDENTITY;

-- 4. Make the surrogate column the primary key (only if the table has no PK yet).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'shift4.shipments'::regclass
          AND contype = 'p'
    ) THEN
        ALTER TABLE shift4.shipments
            ADD CONSTRAINT shipments_pkey PRIMARY KEY (id);
    END IF;
END $$;

-- shift4_shipment_id remains a plain BIGINT column (still NOT NULL; Shift4 always
-- sends a value, 0 at order creation). It is no longer unique, so many orders may
-- share shift4_shipment_id = 0 without collision.
COMMENT ON COLUMN shift4.shipments.shift4_shipment_id IS
    'Shift4 ShipmentID. 0 at order-creation time (no real shipment yet); populated with a real id if a shipment is later created and the order re-ingests. No longer unique -- see ADR-0023.';
