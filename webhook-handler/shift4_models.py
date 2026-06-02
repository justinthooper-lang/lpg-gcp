"""Pydantic models for Shift4Shop webhook payloads.

Shift4's webhook body matches its REST API order schema. Per ADR-0009,
we model only the fields we ingest into shift4.* tables, plus a few
that drive computed values (totals, ship-to fallbacks). Fields we
don't model are still preserved in shift4.orders.raw_payload (JSONB)
for forensics.

Shift4 uses PascalCase JSON field names. Python attribute names are
snake_case; aliases map between them. `populate_by_name=True` allows
either form when constructing the model (useful for tests).
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class Shift4OrderItem(BaseModel):
    """One line item within an order."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    sku: str = Field(alias="ItemID")
    description: Optional[str] = Field(default=None, alias="ItemDescription")
    quantity: float = Field(alias="ItemQuantity")
    unit_price: float = Field(alias="ItemUnitPrice")
    item_unit_cost_shift4: Optional[float] = Field(default=None, alias="ItemUnitCost")


class Shift4Shipment(BaseModel):
    """One shipment within an order. May be empty at order creation time;
    populated when LPG creates a shipment in Shift4."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    shift4_shipment_id: int = Field(alias="ShipmentID")
    ship_first_name: Optional[str] = Field(default=None, alias="ShipmentFirstName")
    ship_last_name: Optional[str] = Field(default=None, alias="ShipmentLastName")
    ship_company: Optional[str] = Field(default=None, alias="ShipmentCompany")
    ship_address: Optional[str] = Field(default=None, alias="ShipmentAddress")
    ship_address2: Optional[str] = Field(default=None, alias="ShipmentAddress2")
    ship_city: Optional[str] = Field(default=None, alias="ShipmentCity")
    ship_state: Optional[str] = Field(default=None, alias="ShipmentState")
    ship_zip: Optional[str] = Field(default=None, alias="ShipmentZipCode")
    ship_country: Optional[str] = Field(default=None, alias="ShipmentCountry")
    ship_phone: Optional[str] = Field(default=None, alias="ShipmentPhone")
    ship_email: Optional[str] = Field(default=None, alias="ShipmentEmail")
    shipment_method_id: Optional[int] = Field(default=None, alias="ShipmentMethodID")
    shipment_method_name: Optional[str] = Field(default=None, alias="ShipmentMethodName")
    customer_shipping_cost: Optional[float] = Field(default=None, alias="ShipmentCost")
    tracking_code: Optional[str] = Field(default=None, alias="ShipmentTrackingCode")
    shipped_date: Optional[str] = Field(default=None, alias="ShipmentShippedDate")


class Shift4OrderPayload(BaseModel):
    """The full Order New webhook payload from Shift4Shop.

    Field naming follows Shift4's REST schema; aliases map PascalCase
    JSON to snake_case Python. All non-required fields are optional —
    Shift4 sometimes omits fields, and our schema accepts NULL for
    them per ADR-0009.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    # Identity
    shift4_order_id: int = Field(alias="OrderID")
    invoice_number_prefix: Optional[str] = Field(default=None, alias="InvoiceNumberPrefix")
    invoice_number: Optional[int] = Field(default=None, alias="InvoiceNumber")
    customer_id: Optional[int] = Field(default=0, alias="CustomerID")
    order_date: Optional[datetime] = Field(default=None, alias="OrderDate")
    order_status_id: int = Field(alias="OrderStatusID")

    # Billing address (flat fields on the order)
    bill_first_name: Optional[str] = Field(default=None, alias="BillingFirstName")
    bill_last_name: Optional[str] = Field(default=None, alias="BillingLastName")
    bill_company: Optional[str] = Field(default=None, alias="BillingCompany")
    bill_address: Optional[str] = Field(default=None, alias="BillingAddress")
    bill_address2: Optional[str] = Field(default=None, alias="BillingAddress2")
    bill_city: Optional[str] = Field(default=None, alias="BillingCity")
    bill_state: Optional[str] = Field(default=None, alias="BillingState")
    bill_zip: Optional[str] = Field(default=None, alias="BillingZipCode")
    bill_country: Optional[str] = Field(default=None, alias="BillingCountry")
    bill_phone: Optional[str] = Field(default=None, alias="BillingPhoneNumber")
    bill_email: Optional[str] = Field(default=None, alias="BillingEmail")

    # Ship-to address (flat fields on the order — see ADR-0009)
    ship_to_first_name: Optional[str] = Field(default=None, alias="ShipToFirstName")
    ship_to_last_name: Optional[str] = Field(default=None, alias="ShipToLastName")
    ship_to_company: Optional[str] = Field(default=None, alias="ShipToCompany")
    ship_to_address: Optional[str] = Field(default=None, alias="ShipToAddress")
    ship_to_address2: Optional[str] = Field(default=None, alias="ShipToAddress2")
    ship_to_city: Optional[str] = Field(default=None, alias="ShipToCity")
    ship_to_state: Optional[str] = Field(default=None, alias="ShipToState")
    ship_to_zip: Optional[str] = Field(default=None, alias="ShipToZipCode")
    ship_to_country: Optional[str] = Field(default=None, alias="ShipToCountry")
    ship_to_phone: Optional[str] = Field(default=None, alias="ShipToPhoneNumber")

    # Totals (raw values from Shift4; subtotal we compute from items)
    order_amount: Optional[float] = Field(default=None, alias="OrderAmount")
    sales_tax: Optional[float] = Field(default=None, alias="SalesTax")
    sales_tax_2: Optional[float] = Field(default=None, alias="SalesTax2")
    sales_tax_3: Optional[float] = Field(default=None, alias="SalesTax3")
    order_discount: Optional[float] = Field(default=None, alias="OrderDiscount")
    invoice_shipping: Optional[float] = Field(default=None, alias="InvoiceShipping")

    # Free-text — Shift4 actually sends "CustomerComments" (verified
    # against real webhook 2026-06-02), not "Comments".
    comments: Optional[str] = Field(default=None, alias="CustomerComments")

    # Nested arrays
    shipment_list: list[Shift4Shipment] = Field(default_factory=list, alias="ShipmentList")
    order_item_list: list[Shift4OrderItem] = Field(default_factory=list, alias="OrderItemList")


# OrderStatusID → text status mapping per ADR-0009. Only these statuses
# are ingested; anything else is filtered at the webhook layer.
ORDER_STATUS_MAP: dict[int, str] = {
    1: "New",
    2: "Processing",
    4: "Shipped",
    21: "Quote",
}
