"""Pure purchase-order builder for Crown POs (ADR-0018).

Given a customer order (header + line items) plus the BOM and pricing data needed
to resolve it, produces an in-memory ``PurchaseOrder`` (header + lines) ready for
the repository layer to persist. **No database access happens here** — this module
is pure and unit-testable, mirroring the ``crown_invoice_parser`` / ``writer`` split.

Explosion rule (ADR-0018 invariant): each order line item produces exactly **one**
PO line. If its SKU has rows in ``lpg.product_components`` it is a *combo* — the one
line carries the component codes joined with '/' as its Product ID (e.g.
``20012-WH-XX/98006-P``, Crown's "converted id" convention) and the **summed**
component cost; only the SKU value is exploded, not the line. Otherwise the SKU is a
*passthrough* (the LPG SKU **is** the Crown SKU) — emitted verbatim, priced from its
own ``vendor_skus`` row. Either way the line's description is Shift4's own order-item
description, verbatim.

Pricing (ADR-0018 Decision 8): ``vendor_skus.unit_cost`` is the single source of
truth. A line can only go on the PO if it has a real cost; ``unit_cost`` is nullable
in ``vendor_skus`` (e.g. call-for-quote SKUs), and the DB CHECK constraint forbids a
product line with a null cost. So any SKU that cannot be fully priced — a passthrough
with no ``vendor_skus`` row, or a combo where *any* component lacks a cost — is **not**
emitted as a line; it is collected in ``unpriced_skus`` for the caller to surface.
(This is the data-quality failure mode ADR-0018 flags for new/unmapped combos.)

Fees (ADR-0018 Q2 = manual): carried as explicit values supplied by the caller,
appended as ``is_fee`` lines with a dedicated ``amount`` and no product fields.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class OrderItem:
    """One line of the customer's order (from ``shift4.order_items``).

    ``description`` is Shift4's own item description (e.g. "12 inch white acrylic
    with 6 in neck") — it becomes the PO line's description verbatim, for both
    combo and passthrough lines.
    """
    sku: str
    quantity: int
    description: str | None = None


@dataclass(frozen=True)
class Component:
    """A resolved vendor SKU: either a BOM component of a combo, or a
    passthrough SKU's own ``vendor_skus`` row.

    ``quantity_per`` is how many of this component go into one parent unit
    (``product_components.quantity``; always 1 for a passthrough).
    """
    vendor_sku_id: int
    vendor_sku_code: str
    description: str | None
    unit_cost: Decimal | None
    quantity_per: int
    sort_order: int


@dataclass(frozen=True)
class ShipTo:
    """Ship-to snapshot for the PO header (dropship: the end customer)."""
    name: str
    company: str | None
    street: str | None
    city_line: str | None
    phone: str | None


@dataclass(frozen=True)
class Fee:
    """A manual fee line, e.g. ``Fee("Order Fee", Decimal("15.00"))``."""
    description: str
    amount: Decimal


# --------------------------------------------------------------------------- #
# Outputs
# --------------------------------------------------------------------------- #
@dataclass
class POLine:
    """One line on the generated PO. A product line fills the product fields and
    leaves ``amount`` null; a fee line fills ``amount`` and ``description`` only.
    Shape matches the ``chk_purchase_order_lines_kind`` DB constraint.
    """
    is_fee: bool
    sort_order: int
    vendor_sku_id: int | None = None
    vendor_sku_code: str | None = None
    description: str | None = None
    quantity: int | None = None
    unit_cost: Decimal | None = None
    amount: Decimal | None = None


@dataclass
class PurchaseOrder:
    po_number: str
    shift4_order_id: int
    vendor_id: int
    ship_to: ShipTo
    comments: str | None
    lines: list[POLine]
    unpriced_skus: list[str]


# --------------------------------------------------------------------------- #
# Builder
# --------------------------------------------------------------------------- #
def build_purchase_order(
    *,
    po_number: str,
    shift4_order_id: int,
    vendor_id: int,
    order_items: list[OrderItem],
    bom_map: dict[str, list[Component]],
    passthrough_prices: dict[str, Component],
    ship_to: ShipTo,
    comments: str | None = None,
    fees: list[Fee] | None = None,
) -> PurchaseOrder:
    """Build a PurchaseOrder from an order and its resolved BOM/pricing data.

    ``bom_map`` maps a combo SKU to its component list (the exception list — only
    SKUs that decompose appear here). ``passthrough_prices`` maps a SKU to its own
    vendor SKU, used for any order SKU not present in ``bom_map``.
    """
    lines: list[POLine] = []
    unpriced: list[str] = []
    sort = 0

    for item in order_items:
        sku = item.sku

        if sku in bom_map:  # combo -> ONE line, SKU value exploded + cost summed
            components = sorted(bom_map[sku], key=lambda c: c.sort_order)
            # A kit is all-or-nothing: if any component can't be priced, the
            # whole combo is unpriceable (don't emit a mis-priced line).
            if any(c.unit_cost is None for c in components):
                unpriced.append(sku)
                continue
            sort += 1
            lines.append(
                POLine(
                    is_fee=False,
                    sort_order=sort,
                    # Composite: not a single vendor SKU, so no vendor_sku_id.
                    vendor_sku_id=None,
                    # Component codes joined in sort_order, e.g.
                    # "20012-WH-XX/98006-P" — Crown's "converted id" convention.
                    vendor_sku_code="/".join(c.vendor_sku_code for c in components),
                    # Description is Shift4's item description, verbatim.
                    description=item.description,
                    quantity=item.quantity,
                    # Summed component cost per unit (each component qty_per).
                    unit_cost=sum(
                        (c.unit_cost * c.quantity_per for c in components),
                        Decimal("0"),
                    ),
                )
            )
        else:  # passthrough -> one line, priced from its own vendor SKU
            comp = passthrough_prices.get(sku)
            if comp is None or comp.unit_cost is None:
                unpriced.append(sku)
                continue
            sort += 1
            lines.append(
                POLine(
                    is_fee=False,
                    sort_order=sort,
                    vendor_sku_id=comp.vendor_sku_id,
                    vendor_sku_code=comp.vendor_sku_code,
                    description=item.description,
                    quantity=item.quantity * comp.quantity_per,
                    unit_cost=comp.unit_cost,
                )
            )

    for fee in fees or []:
        sort += 1
        lines.append(
            POLine(
                is_fee=True,
                sort_order=sort,
                description=fee.description,
                amount=fee.amount,
            )
        )

    return PurchaseOrder(
        po_number=po_number,
        shift4_order_id=shift4_order_id,
        vendor_id=vendor_id,
        ship_to=ship_to,
        comments=comments,
        lines=lines,
        unpriced_skus=unpriced,
    )
