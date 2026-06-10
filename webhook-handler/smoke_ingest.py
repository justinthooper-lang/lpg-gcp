"""Smoke test for ingest.ingest_order.

Reads the test_payload.json fixture, validates it through Pydantic,
inserts it via ingest_order, then queries the DB to confirm.

Run: python smoke_ingest.py
"""

import json

from lpg_common.db import get_connection
from ingest import ingest_order
from shift4_models import Shift4OrderPayload

with open("test_payload.json") as f:
    raw = json.load(f)

payload = Shift4OrderPayload(**raw)
print("Parsed:", payload.shift4_order_id, payload.bill_first_name, payload.bill_last_name)

result = ingest_order(payload)
print("Ingested:", result)

# Verify what's in the DB
with get_connection() as conn:
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT shift4_order_id, shift4_customer_id, order_status, "
            "subtotal, tax, shipping_cost, grand_total, invoice_number "
            "FROM shift4.orders WHERE shift4_order_id = %s",
            (payload.shift4_order_id,),
        )
        row = cur.fetchone()
        print("Order in DB:", row)

        cur.execute(
            "SELECT sku, quantity, unit_price FROM shift4.order_items "
            "WHERE shift4_order_id = %s",
            (payload.shift4_order_id,),
        )
        print("Items in DB:", cur.fetchall())
    finally:
        cur.close()
        