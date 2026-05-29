"""Quick smoke test for shift4_models parsing.

Run with: python test_models.py
"""

from shift4_models import Shift4OrderPayload

payload = {
    "OrderID": 31990,
    "InvoiceNumberPrefix": "PO",
    "InvoiceNumber": 31990,
    "CustomerID": 0,
    "OrderStatusID": 1,
    "BillingFirstName": "Patricia",
    "BillingLastName": "Dougherty",
    "BillingEmail": "PDOUGHERTY@example.com",
    "OrderAmount": 156.78,
    "OrderItemList": [
        {"ItemID": "20012-CL-4F", "ItemQuantity": 12, "ItemUnitPrice": 13.99}
    ],
    "ShipmentList": [],
}

order = Shift4OrderPayload(**payload)
print("Parsed order:", order.shift4_order_id, order.bill_first_name, order.bill_last_name)
print("Item count:", len(order.order_item_list))
print("First item:", order.order_item_list[0].sku, order.order_item_list[0].quantity)
