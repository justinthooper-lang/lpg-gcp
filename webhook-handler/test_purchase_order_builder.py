"""Standalone tests for purchase_order_builder (no DB, no pytest needed).

Run: python test_purchase_order_builder.py
Exits 0 if all assertions pass.
"""

from decimal import Decimal

from purchase_order_builder import (
    Component,
    Fee,
    OrderItem,
    ShipTo,
    build_purchase_order,
)

SHIP = ShipTo(
    name="Jane Doe",
    company="Tait Towers",
    street="123 Main St",
    city_line="Lititz, PA 17543",
    phone="717-555-0100",
)

# Realistic BOM: 20012-WH-6F decomposes into globe + neck (different SKUs).
BOM = {
    "20012-WH-6F": [
        Component(101, "20012-WH-XX", "12in white globe", Decimal("3.00"), 1, 1),
        Component(102, "98006-P", "6in neck", Decimal("18.40"), 1, 2),
    ],
    # A combo where one component has no cost (call-for-quote) -> unpriceable.
    "20099-WH-XX": [
        Component(201, "20099-WH-XX", "special globe", None, 1, 1),
        Component(202, "98006-P", "6in neck", Decimal("18.40"), 1, 2),
    ],
}

# Passthrough prices: SKU == its own Crown SKU.
PASS = {
    "20012-CL-4F": Component(193, "20012-CL-4F", "clear 4in", Decimal("12.40"), 1, 1),
}


def test_combo_collapses_to_one_line_with_joined_sku_and_summed_cost():
    po = build_purchase_order(
        po_number="PO32163",
        shift4_order_id=32163,
        vendor_id=1,
        order_items=[OrderItem("20012-WH-6F", 2,
                               description="12 inch white acrylic with 6 in neck")],
        bom_map=BOM,
        passthrough_prices=PASS,
        ship_to=SHIP,
    )
    product_lines = [l for l in po.lines if not l.is_fee]
    assert len(product_lines) == 1, product_lines          # ONE line, not two
    line = product_lines[0]
    assert line.vendor_sku_code == "20012-WH-XX/98006-P"     # joined in sort_order
    assert line.vendor_sku_id is None                        # composite, no single id
    assert line.quantity == 2                                # order qty, not multiplied
    assert line.unit_cost == Decimal("21.40")                # 3.00 + 18.40 summed
    assert line.description == "12 inch white acrylic with 6 in neck"  # Shift4's text
    assert po.unpriced_skus == []
    print("ok: combo -> one line, joined SKU, summed cost, Shift4 description")


def test_passthrough_emits_self():
    po = build_purchase_order(
        po_number="PO32164",
        shift4_order_id=32164,
        vendor_id=1,
        order_items=[OrderItem("20012-CL-4F", 3, description="12 inch clear 4 in neck")],
        bom_map=BOM,
        passthrough_prices=PASS,
        ship_to=SHIP,
    )
    product_lines = [l for l in po.lines if not l.is_fee]
    assert len(product_lines) == 1
    assert product_lines[0].vendor_sku_code == "20012-CL-4F"
    assert product_lines[0].quantity == 3
    assert product_lines[0].unit_cost == Decimal("12.40")
    assert product_lines[0].description == "12 inch clear 4 in neck"  # Shift4's text
    print("ok: passthrough emits itself with Shift4 description")


def test_fees_appended_as_fee_lines():
    po = build_purchase_order(
        po_number="PO32165",
        shift4_order_id=32165,
        vendor_id=1,
        order_items=[OrderItem("20012-CL-4F", 1)],
        bom_map=BOM,
        passthrough_prices=PASS,
        ship_to=SHIP,
        fees=[Fee("Order Fee", Decimal("15.00"))],
    )
    fee_lines = [l for l in po.lines if l.is_fee]
    assert len(fee_lines) == 1
    f = fee_lines[0]
    assert f.description == "Order Fee"
    assert f.amount == Decimal("15.00")
    # Fee line must carry NO product fields (matches the DB CHECK constraint).
    assert f.vendor_sku_id is None and f.quantity is None and f.unit_cost is None
    print("ok: fee appended as a clean fee line")


def test_unknown_passthrough_is_flagged_not_emitted():
    po = build_purchase_order(
        po_number="PO32166",
        shift4_order_id=32166,
        vendor_id=1,
        order_items=[OrderItem("MYSTERY-SKU", 1)],
        bom_map=BOM,
        passthrough_prices=PASS,
        ship_to=SHIP,
    )
    assert po.lines == []
    assert po.unpriced_skus == ["MYSTERY-SKU"]
    print("ok: unknown passthrough flagged, not emitted")


def test_null_cost_combo_is_flagged_not_half_emitted():
    po = build_purchase_order(
        po_number="PO32167",
        shift4_order_id=32167,
        vendor_id=1,
        order_items=[OrderItem("20099-WH-XX", 1)],
        bom_map=BOM,
        passthrough_prices=PASS,
        ship_to=SHIP,
    )
    assert po.lines == []                      # not a single half-priced line
    assert po.unpriced_skus == ["20099-WH-XX"]
    print("ok: null-cost combo flagged whole, no partial kit")


def test_sort_order_is_monotonic_across_mixed_lines():
    po = build_purchase_order(
        po_number="PO32168",
        shift4_order_id=32168,
        vendor_id=1,
        order_items=[OrderItem("20012-WH-6F", 1), OrderItem("20012-CL-4F", 1)],
        bom_map=BOM,
        passthrough_prices=PASS,
        ship_to=SHIP,
        fees=[Fee("Order Fee", Decimal("15.00"))],
    )
    orders = [l.sort_order for l in po.lines]
    assert orders == [1, 2, 3], orders       # 1 combo line + 1 passthrough + 1 fee
    print("ok: sort_order monotonic across product + fee lines")


if __name__ == "__main__":
    test_combo_collapses_to_one_line_with_joined_sku_and_summed_cost()
    test_passthrough_emits_self()
    test_fees_appended_as_fee_lines()
    test_unknown_passthrough_is_flagged_not_emitted()
    test_null_cost_combo_is_flagged_not_half_emitted()
    test_sort_order_is_monotonic_across_mixed_lines()
    print("\nALL PASSED")
