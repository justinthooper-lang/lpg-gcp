"""DB ingestion for validated Shift4 order payloads.

Takes a Shift4OrderPayload and writes it to the shift4.* tables in
a single transaction. Computes derived totals (subtotal, summed tax,
summed shipping) per ADR-0009.

Known limitation: order_items INSERTs will fail if a SKU isn't in
shift4.products (FK violation). ADR-0005 documents this race condition;
Layer 3.5 will add product-stub auto-creation.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from db import get_connection
from shift4_models import ORDER_STATUS_MAP, Shift4OrderPayload


def _customer_id_for(payload: Shift4OrderPayload) -> str:
    """Return the canonical shift4_customer_id, synthesizing guest IDs."""
    cid = payload.customer_id or 0
    if cid == 0:
        return f"guest-{payload.shift4_order_id}"
    return str(cid)


def _compute_totals(payload: Shift4OrderPayload) -> dict[str, Decimal]:
    """Compute order totals from the payload per ADR-0009."""
    subtotal = sum(
        (Decimal(str(item.unit_price)) * Decimal(item.quantity))
        for item in payload.order_item_list
    )
    tax = sum(
        Decimal(str(t or 0))
        for t in (payload.sales_tax, payload.sales_tax_2, payload.sales_tax_3)
    )
    # Shipping: sum ShipmentList costs, fall back to InvoiceShipping
    shipping = sum(
        Decimal(str(s.customer_shipping_cost or 0))
        for s in payload.shipment_list
    )
    if shipping == 0 and payload.invoice_shipping:
        shipping = Decimal(str(payload.invoice_shipping))

    return {
        "subtotal": Decimal(subtotal).quantize(Decimal("0.01")),
        "tax": Decimal(tax).quantize(Decimal("0.01")),
        "shipping_cost": Decimal(shipping).quantize(Decimal("0.01")),
        "discount": Decimal(str(payload.order_discount or 0)).quantize(Decimal("0.01")),
        "grand_total": Decimal(str(payload.order_amount or 0)).quantize(Decimal("0.01")),
    }


def _invoice_number(payload: Shift4OrderPayload) -> str | None:
    """Build the human-readable invoice number (e.g., 'PO31990')."""
    prefix = payload.invoice_number_prefix or ""
    number = payload.invoice_number
    if number is None:
        return None
    return f"{prefix}{number}".strip() or None


def ingest_order(payload: Shift4OrderPayload) -> dict[str, Any]:
    """Write a validated Shift4 order to the database.

    All writes happen in one transaction. Returns a summary dict on
    success. Raises on DB error (caller decides response).
    """
    customer_id = _customer_id_for(payload)
    is_guest = customer_id.startswith("guest-")
    status_text = ORDER_STATUS_MAP[payload.order_status_id]
    totals = _compute_totals(payload)
    raw_payload_json = payload.model_dump_json(by_alias=True)

    with get_connection() as conn:
        cur = conn.cursor()
        try:
            # 1. Customer — upsert
            cur.execute(
                """
                INSERT INTO shift4.customers (
                    shift4_customer_id, first_name, last_name, company_name,
                    email, phone, raw_payload
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (shift4_customer_id) DO UPDATE SET
                    first_name   = EXCLUDED.first_name,
                    last_name    = EXCLUDED.last_name,
                    company_name = EXCLUDED.company_name,
                    email        = EXCLUDED.email,
                    phone        = EXCLUDED.phone,
                    updated_at   = NOW()
                """,
                (
                    customer_id,
                    payload.bill_first_name,
                    payload.bill_last_name,
                    payload.bill_company,
                    (payload.bill_email or "").lower() or None,
                    payload.bill_phone,
                    raw_payload_json,
                ),
            )

            # 2. Order — upsert
            cur.execute(
                """
                INSERT INTO shift4.orders (
                    shift4_order_id, shift4_customer_id, order_date, order_status,
                    bill_first_name, bill_last_name, bill_company,
                    bill_address, bill_address2, bill_city, bill_state, bill_zip,
                    bill_country, bill_phone, bill_email,
                    ship_to_first_name, ship_to_last_name, ship_to_company,
                    ship_to_address, ship_to_address2, ship_to_city, ship_to_state,
                    ship_to_zip, ship_to_country, ship_to_phone,
                    subtotal, tax, shipping_cost, discount, grand_total,
                    invoice_number, comments, raw_payload
                ) VALUES (
                    %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s, %s
                )
                ON CONFLICT (shift4_order_id) DO UPDATE SET
                    order_status      = EXCLUDED.order_status,
                    grand_total       = EXCLUDED.grand_total,
                    subtotal          = EXCLUDED.subtotal,
                    tax               = EXCLUDED.tax,
                    shipping_cost     = EXCLUDED.shipping_cost,
                    discount          = EXCLUDED.discount,
                    raw_payload       = EXCLUDED.raw_payload,
                    updated_at        = NOW()
                """,
                (
                    payload.shift4_order_id, customer_id, payload.order_date, status_text,
                    payload.bill_first_name, payload.bill_last_name, payload.bill_company,
                    payload.bill_address, payload.bill_address2, payload.bill_city,
                    payload.bill_state, payload.bill_zip, payload.bill_country,
                    payload.bill_phone, (payload.bill_email or "").lower() or None,
                    payload.ship_to_first_name, payload.ship_to_last_name,
                    payload.ship_to_company, payload.ship_to_address,
                    payload.ship_to_address2, payload.ship_to_city,
                    payload.ship_to_state, payload.ship_to_zip,
                    payload.ship_to_country, payload.ship_to_phone,
                    totals["subtotal"], totals["tax"], totals["shipping_cost"],
                    totals["discount"], totals["grand_total"],
                    _invoice_number(payload), payload.comments, raw_payload_json,
                ),
            )

            # 3. Order items — replace
            # First, create stub product rows for any SKUs we haven't seen.
            # Real product data fills in later via Product webhook (ADR-0010).
            for item in payload.order_item_list:
                cur.execute(
                    """
                    INSERT INTO shift4.products (sku, name)
                    VALUES (%s, %s)
                    ON CONFLICT (sku) DO NOTHING
                    """,
                    (item.sku, item.description or item.sku),
                )
            cur.execute(
                "DELETE FROM shift4.order_items WHERE shift4_order_id = %s",
                (payload.shift4_order_id,),
            )
            for item in payload.order_item_list:
                cur.execute(
                "DELETE FROM shift4.order_items WHERE shift4_order_id = %s",
                (payload.shift4_order_id,),
            )
            for item in payload.order_item_list:
                cur.execute(
                    """
                    INSERT INTO shift4.order_items (
                        shift4_order_id, sku, quantity, unit_price,
                        item_unit_cost_shift4
                    ) VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        payload.shift4_order_id,
                        item.sku,
                        item.quantity,
                        Decimal(str(item.unit_price)).quantize(Decimal("0.01")),
                        Decimal(str(item.item_unit_cost_shift4 or 0)).quantize(Decimal("0.01"))
                        if item.item_unit_cost_shift4 is not None else None,
                    ),
                )

            # 4. Shipments — replace
            cur.execute(
                "DELETE FROM shift4.shipments WHERE shift4_order_id = %s",
                (payload.shift4_order_id,),
            )
            for shipment in payload.shipment_list:
                cur.execute(
                    """
                    INSERT INTO shift4.shipments (
                        shift4_shipment_id, shift4_order_id,
                        ship_first_name, ship_last_name, ship_company,
                        ship_address, ship_address2, ship_city, ship_state,
                        ship_zip, ship_country, ship_phone, ship_email,
                        shipment_method_id, shipment_method_name,
                        customer_shipping_cost, tracking_code
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        shipment.shift4_shipment_id, payload.shift4_order_id,
                        shipment.ship_first_name, shipment.ship_last_name,
                        shipment.ship_company, shipment.ship_address,
                        shipment.ship_address2, shipment.ship_city,
                        shipment.ship_state, shipment.ship_zip,
                        shipment.ship_country, shipment.ship_phone,
                        shipment.ship_email,
                        shipment.shipment_method_id, shipment.shipment_method_name,
                        Decimal(str(shipment.customer_shipping_cost or 0)).quantize(Decimal("0.01"))
                        if shipment.customer_shipping_cost is not None else None,
                        shipment.tracking_code,
                    ),
                )

        finally:
            cur.close()

    return {
        "shift4_order_id": payload.shift4_order_id,
        "shift4_customer_id": customer_id,
        "is_guest": is_guest,
        "status": status_text,
        "items_inserted": len(payload.order_item_list),
        "shipments_inserted": len(payload.shipment_list),
        "totals": {k: str(v) for k, v in totals.items()},
    }
    