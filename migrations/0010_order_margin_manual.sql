-- migrations/0010_order_margin_manual.sql
-- ADR-0025: manual margin entry as a gap-filler for orders without a Crown invoice.
--
-- v_order_margins (ADR-0024) yields true margins for orders matched to a Crown
-- invoice, and NULL for the rest. This adds the ability to MANUALLY enter a
-- supplier cost + freight for an order that has no matched invoice yet, so it
-- still gets a margin (and counts in the dashboard).
--
-- Precedence is strict and safe: a real Crown invoice ALWAYS wins. Manual entry
-- only applies when has_invoice = false. It never overrides invoice-true data --
-- so when the real Crown invoice later arrives via sync and matches, the computed
-- value takes precedence automatically and the manual row is simply ignored
-- (retained, not deleted, so nothing is lost). margin_source makes the basis
-- explicit: 'invoice' | 'manual' | 'none'.
--
-- Profit / shipping_differential always RECOMPUTE from whichever cost basis wins,
-- so the arithmetic stays internally consistent (profit = grand_total - cost -
-- freight; shipping_differential = shipping_cost - freight) regardless of source.

-- 1. Manual entry table: one row per order, only for orders being hand-filled.
CREATE TABLE IF NOT EXISTS lpg.order_margin_manual (
    shift4_order_id      BIGINT       PRIMARY KEY
                                       REFERENCES shift4.orders(shift4_order_id),
    manual_supplier_cost NUMERIC(12,2) CHECK (manual_supplier_cost >= 0),
    manual_freight       NUMERIC(12,2) CHECK (manual_freight >= 0),
    note                 TEXT,
    created_at           TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ  NOT NULL DEFAULT now()
);

COMMENT ON TABLE lpg.order_margin_manual IS
    'Manually-entered supplier cost / freight for orders with no matched Crown invoice (ADR-0025). Gap-filler only: ignored whenever v_order_margins has a real invoice for the order. NULL column = not provided.';

-- keep updated_at fresh (same trigger fn used elsewhere)
DROP TRIGGER IF EXISTS trg_order_margin_manual_updated_at ON lpg.order_margin_manual;
CREATE TRIGGER trg_order_margin_manual_updated_at
    BEFORE UPDATE ON lpg.order_margin_manual
    FOR EACH ROW EXECUTE FUNCTION lpg.set_updated_at();

-- 2. Rebuild the margin view with invoice -> manual -> none precedence.
DROP VIEW IF EXISTS lpg.v_order_margins;
CREATE VIEW lpg.v_order_margins AS
WITH ranked_invoices AS (
    SELECT
        vi.*,
        ROW_NUMBER() OVER (
            PARTITION BY vi.customer_po_number
            ORDER BY
                (vi.graph_message_id LIKE 'sf-migration:%')::int ASC,
                vi.invoice_date DESC NULLS LAST,
                vi.vendor_invoice_id DESC
        ) AS rn
    FROM lpg.vendor_invoices vi
    WHERE vi.customer_po_number IS NOT NULL
),
best_invoice AS (
    SELECT * FROM ranked_invoices WHERE rn = 1
),
base AS (
    SELECT
        o.shift4_order_id,
        o.invoice_number,
        o.order_date,
        o.order_status,
        o.grand_total,
        o.subtotal,
        o.shipping_cost,
        bi.vendor_invoice_id,
        bi.vendor_invoice_number,
        (bi.graph_message_id LIKE 'sf-migration:%')                  AS invoice_from_salesforce,
        (bi.vendor_invoice_id IS NOT NULL)                           AS has_invoice,
        bi.sale_amount                                               AS invoice_cost,
        (COALESCE(bi.freight_truck, 0) + COALESCE(bi.freight_ups, 0)) AS invoice_freight,
        mm.manual_supplier_cost,
        mm.manual_freight,
        (mm.shift4_order_id IS NOT NULL)                             AS has_manual
    FROM shift4.orders o
    LEFT JOIN best_invoice bi          ON bi.customer_po_number = o.invoice_number
    LEFT JOIN lpg.order_margin_manual mm ON mm.shift4_order_id = o.shift4_order_id
),
resolved AS (
    SELECT
        *,
        -- margin_source: invoice wins; else manual (if both cost+freight given); else none
        CASE
            WHEN has_invoice THEN 'invoice'
            WHEN manual_supplier_cost IS NOT NULL AND manual_freight IS NOT NULL THEN 'manual'
            ELSE 'none'
        END AS margin_source,
        -- effective cost / freight from the winning source
        CASE
            WHEN has_invoice THEN invoice_cost
            WHEN manual_supplier_cost IS NOT NULL AND manual_freight IS NOT NULL THEN manual_supplier_cost
        END AS supplier_cost,
        CASE
            WHEN has_invoice THEN invoice_freight
            WHEN manual_supplier_cost IS NOT NULL AND manual_freight IS NOT NULL THEN manual_freight
        END AS actual_freight
    FROM base
)
SELECT
    shift4_order_id,
    invoice_number,
    order_date,
    order_status,
    grand_total,
    subtotal,
    shipping_cost,
    vendor_invoice_id,
    vendor_invoice_number,
    invoice_from_salesforce,
    has_invoice,
    has_manual,
    margin_source,
    supplier_cost,
    actual_freight,
    -- recompute profit / differential from the effective basis (NULL when source='none')
    CASE WHEN margin_source <> 'none'
         THEN grand_total - COALESCE(supplier_cost, 0) - COALESCE(actual_freight, 0)
    END AS profit,
    CASE WHEN margin_source <> 'none'
         THEN shipping_cost - COALESCE(actual_freight, 0)
    END AS shipping_differential
FROM resolved;

COMMENT ON VIEW lpg.v_order_margins IS
    'Order-level margins. Precedence: matched Crown invoice (margin_source=invoice) > manual entry for unmatched orders (margin_source=manual) > none. profit = grand_total - supplier_cost - actual_freight; shipping_differential = shipping_cost - actual_freight. has_invoice/has_manual/margin_source expose the basis. Manual entry (lpg.order_margin_manual, ADR-0025) never overrides a real invoice. (ADR-0024 base match.)';
