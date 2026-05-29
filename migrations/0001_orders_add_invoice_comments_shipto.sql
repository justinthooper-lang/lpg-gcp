-- Migration 0001: Add invoice_number, comments, and ship-to address columns
-- to shift4.orders.
--
-- Context (see ADR-0009): the existing LPG → Salesforce integration
-- demonstrated that Shift4 sends ship-to address fields on the order
-- itself (ShipTo*) BEFORE any shipment row exists. Order-created
-- webhooks therefore have no shift4.shipments row to write to, but do
-- have ship-to data that needs a home. Also adds InvoiceNumber (the
-- human-readable order identifier like "PO31990") and Comments (customer
-- comments at checkout).

ALTER TABLE shift4.orders
    ADD COLUMN IF NOT EXISTS invoice_number      TEXT,
    ADD COLUMN IF NOT EXISTS comments            TEXT,
    ADD COLUMN IF NOT EXISTS ship_to_first_name  TEXT,
    ADD COLUMN IF NOT EXISTS ship_to_last_name   TEXT,
    ADD COLUMN IF NOT EXISTS ship_to_company     TEXT,
    ADD COLUMN IF NOT EXISTS ship_to_address     TEXT,
    ADD COLUMN IF NOT EXISTS ship_to_address2    TEXT,
    ADD COLUMN IF NOT EXISTS ship_to_city        TEXT,
    ADD COLUMN IF NOT EXISTS ship_to_state       TEXT,
    ADD COLUMN IF NOT EXISTS ship_to_zip         TEXT,
    ADD COLUMN IF NOT EXISTS ship_to_country     TEXT,
    ADD COLUMN IF NOT EXISTS ship_to_phone       TEXT;

CREATE INDEX IF NOT EXISTS idx_orders_invoice_number ON shift4.orders(invoice_number);

COMMENT ON COLUMN shift4.orders.invoice_number IS
    'Human-readable order identifier (InvoiceNumberPrefix + InvoiceNumber from Shift4, e.g. "PO31990"). The shift4_order_id column is the internal numeric ID.';
COMMENT ON COLUMN shift4.orders.comments IS
    'Customer-entered comments at checkout (Shift4 "Comments" field). Free text. Queryable; raw_payload also captures it.';
    