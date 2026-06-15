-- migrations/0006_order_overrides.sql
-- ADR-0021: LPG-owned order field overrides that overlay the read-only
-- shift4.orders mirror.
--
-- Why this exists: shift4.orders is a faithful mirror of Shift4Shop, which is
-- the system of record (source-of-truth rule #1 — webhook writes only, nothing
-- else writes shift4.*). When LPG must CORRECT an order field for a back-office
-- workflow (e.g. a missing or garbled ship-to that a PO needs), the corrected
-- value must NOT be written into shift4.orders — a re-fired webhook upsert would
-- silently clobber it, and the mirror would diverge from the storefront with no
-- record of why. Instead the correction lives here, in an LPG-owned table, and
-- reads overlay the two via lpg.v_orders_effective (COALESCE override over mirror).
--
-- Scope: addresses, contact, and comments only. order_status and the monetary
-- totals are deliberately NOT overridable — totals are mirrored expressly to
-- reconcile customer charges against Crown invoices, and a local override would
-- corrupt that reconciliation. A disputed total is a reconciliation note, not an
-- order edit.
--
-- A NULL override column means "no override" and falls back to the mirror.
-- DELETE the row to revert an order entirely to storefront truth (fully
-- reversible, nothing lost).
--
-- Idempotent: safe to re-run via \i (CREATE TABLE IF NOT EXISTS, CREATE OR
-- REPLACE VIEW, DROP TRIGGER IF EXISTS + CREATE).

-- ---------------------------------------------------------------------------
-- 1. Override table — one row per corrected order.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS lpg.order_overrides (
    shift4_order_id     BIGINT PRIMARY KEY
                            REFERENCES shift4.orders(shift4_order_id)
                            ON DELETE CASCADE,

    -- Billing override (NULL = fall back to shift4.orders)
    bill_first_name     TEXT,
    bill_last_name      TEXT,
    bill_company        TEXT,
    bill_address        TEXT,
    bill_address2       TEXT,
    bill_city           TEXT,
    bill_state          TEXT,
    bill_zip            TEXT,
    bill_country        TEXT,
    bill_phone          TEXT,
    bill_email          TEXT,

    -- Ship-to override (NULL = fall back to shift4.orders)
    ship_to_first_name  TEXT,
    ship_to_last_name   TEXT,
    ship_to_company     TEXT,
    ship_to_address     TEXT,
    ship_to_address2    TEXT,
    ship_to_city        TEXT,
    ship_to_state       TEXT,
    ship_to_zip         TEXT,
    ship_to_country     TEXT,
    ship_to_phone       TEXT,

    -- Comments override
    comments            TEXT,

    -- Provenance — who corrected the order and why (auditability)
    override_reason     TEXT,
    overridden_by       TEXT,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

DROP TRIGGER IF EXISTS trg_order_overrides_updated_at ON lpg.order_overrides;
CREATE TRIGGER trg_order_overrides_updated_at
    BEFORE UPDATE ON lpg.order_overrides
    FOR EACH ROW EXECUTE FUNCTION lpg.set_updated_at();

COMMENT ON TABLE lpg.order_overrides IS
    'LPG-owned corrections overlaying the read-only shift4.orders mirror (ADR-0021). NULL column = no override (falls back to mirror). Addresses/contact/comments only; totals and status are not overridable. Read via lpg.v_orders_effective.';

-- ---------------------------------------------------------------------------
-- 2. Effective view — override over mirror. Read paths that need the corrected
--    value (PO builder, admin order detail) read THIS, not shift4.orders.
--    Non-overridable columns pass through unchanged.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW lpg.v_orders_effective AS
SELECT
    o.shift4_order_id,
    o.shift4_customer_id,
    o.order_date,
    o.order_status,

    COALESCE(ov.bill_first_name, o.bill_first_name) AS bill_first_name,
    COALESCE(ov.bill_last_name,  o.bill_last_name)  AS bill_last_name,
    COALESCE(ov.bill_company,    o.bill_company)    AS bill_company,
    COALESCE(ov.bill_address,    o.bill_address)    AS bill_address,
    COALESCE(ov.bill_address2,   o.bill_address2)   AS bill_address2,
    COALESCE(ov.bill_city,       o.bill_city)       AS bill_city,
    COALESCE(ov.bill_state,      o.bill_state)      AS bill_state,
    COALESCE(ov.bill_zip,        o.bill_zip)        AS bill_zip,
    COALESCE(ov.bill_country,    o.bill_country)    AS bill_country,
    COALESCE(ov.bill_phone,      o.bill_phone)      AS bill_phone,
    COALESCE(ov.bill_email,      o.bill_email)      AS bill_email,

    o.subtotal,
    o.tax,
    o.shipping_cost,
    o.discount,
    o.grand_total,

    o.invoice_number,

    COALESCE(ov.comments, o.comments) AS comments,

    COALESCE(ov.ship_to_first_name, o.ship_to_first_name) AS ship_to_first_name,
    COALESCE(ov.ship_to_last_name,  o.ship_to_last_name)  AS ship_to_last_name,
    COALESCE(ov.ship_to_company,    o.ship_to_company)    AS ship_to_company,
    COALESCE(ov.ship_to_address,    o.ship_to_address)    AS ship_to_address,
    COALESCE(ov.ship_to_address2,   o.ship_to_address2)   AS ship_to_address2,
    COALESCE(ov.ship_to_city,       o.ship_to_city)       AS ship_to_city,
    COALESCE(ov.ship_to_state,      o.ship_to_state)      AS ship_to_state,
    COALESCE(ov.ship_to_zip,        o.ship_to_zip)        AS ship_to_zip,
    COALESCE(ov.ship_to_country,    o.ship_to_country)    AS ship_to_country,
    COALESCE(ov.ship_to_phone,      o.ship_to_phone)      AS ship_to_phone,

    o.raw_payload,
    o.created_at,
    o.updated_at,

    (ov.shift4_order_id IS NOT NULL) AS has_override
FROM shift4.orders o
LEFT JOIN lpg.order_overrides ov USING (shift4_order_id);

COMMENT ON VIEW lpg.v_orders_effective IS
    'shift4.orders with lpg.order_overrides applied (COALESCE override over mirror). has_override flags whether a correction row exists. Source-of-truth-preserving read surface (ADR-0021).';
