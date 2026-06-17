-- migrations/0009_order_margins_view.sql
-- ADR-0024: order-level margin view (direct order<->invoice match, no PO required).
--
-- True margin needs the actual Crown cost + freight, which live on
-- lpg.vendor_invoices. The match key is the customer PO number Crown prints on its
-- invoice, which equals the LPG order's invoice_number:
--     shift4.orders.invoice_number = lpg.vendor_invoices.customer_po_number
-- The purchase_orders row is NOT required for this match -- it only ever carried
-- the same key. Skipping it lets historical orders (no generated PO) still get
-- true margins from migrated/ingested invoices.
--
-- Best-invoice-per-order: a PO may have both a real Crown invoice (ingested from
-- the PDF via the daily sync) and an 'sf:' backstop (migrated from Salesforce,
-- graph_message_id LIKE 'sf-migration:%'). The real Crown invoice is
-- authoritative and wins; the SF row is a fallback until the real one arrives.
--
-- Freight: each order ships truck XOR UPS, so only one freight column is ever
-- populated; actual freight = COALESCE(truck,0)+COALESCE(ups,0). (Migrated rows
-- put the combined figure in freight_truck.)
--
-- Margin formulas (reconciled against the Salesforce export's own columns):
--   profit               = grand_total - sale_amount - actual_freight
--   shipping_differential = shipping_cost - actual_freight   (negative = undercharged)
--
-- Orders with no matched invoice yield NULL margins (never a fabricated number),
-- and are identified by has_invoice = false so the dashboard can segment
-- "true margin" from "cost pending".

CREATE OR REPLACE VIEW lpg.v_order_margins AS
WITH ranked_invoices AS (
    SELECT
        vi.*,
        ROW_NUMBER() OVER (
            PARTITION BY vi.customer_po_number
            ORDER BY
                -- real Crown invoices (not sf-migration) rank first
                (vi.graph_message_id LIKE 'sf-migration:%')::int ASC,
                vi.invoice_date DESC NULLS LAST,
                vi.vendor_invoice_id DESC
        ) AS rn
    FROM lpg.vendor_invoices vi
    WHERE vi.customer_po_number IS NOT NULL
),
best_invoice AS (
    SELECT * FROM ranked_invoices WHERE rn = 1
)
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
    (bi.graph_message_id LIKE 'sf-migration:%')          AS invoice_from_salesforce,
    (bi.vendor_invoice_id IS NOT NULL)                   AS has_invoice,
    bi.sale_amount                                       AS supplier_cost,
    (COALESCE(bi.freight_truck, 0) + COALESCE(bi.freight_ups, 0)) AS actual_freight,
    -- margins: NULL when no invoice (no fabricated cost)
    CASE WHEN bi.vendor_invoice_id IS NOT NULL
         THEN o.grand_total
            - COALESCE(bi.sale_amount, 0)
            - (COALESCE(bi.freight_truck, 0) + COALESCE(bi.freight_ups, 0))
    END                                                  AS profit,
    CASE WHEN bi.vendor_invoice_id IS NOT NULL
         THEN o.shipping_cost
            - (COALESCE(bi.freight_truck, 0) + COALESCE(bi.freight_ups, 0))
    END                                                  AS shipping_differential
FROM shift4.orders o
LEFT JOIN best_invoice bi
       ON bi.customer_po_number = o.invoice_number;

COMMENT ON VIEW lpg.v_order_margins IS
    'Order-level true margin via direct order.invoice_number = vendor_invoices.customer_po_number match (no PO required; ADR-0024). Prefers real Crown invoices over sf-migration backstops. profit = grand_total - supplier_cost - actual_freight; shipping_differential = shipping_cost - actual_freight (negative = undercharged). NULL margins where has_invoice = false.';
