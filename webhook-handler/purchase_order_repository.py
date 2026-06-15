"""Repository layer for purchase-order generation (ADR-0018).

The DB half of the PO-gen split — the ``crown_invoice_writer`` analog. Two jobs:

* **fetch** — read a Shift4 order (header + items) and resolve the BOM and pricing
  data into the exact shapes ``purchase_order_builder`` consumes (``bom_map`` and
  ``passthrough_prices``).
* **persist** — write a built ``PurchaseOrder`` into ``lpg.purchase_orders`` /
  ``lpg.purchase_order_lines``, idempotent on ``po_number`` (regeneration updates
  in place).

Like ``crown_invoice_writer``, every function takes an OPEN pg8000 connection; the
caller owns the transaction (wrap in ``lpg_common.db.get_connection()`` so the header
and all lines commit atomically or roll back together). No DB connection is opened
here, which also keeps these functions testable against any pg8000 connection.

PO number sources from ``shift4.orders.invoice_number`` (e.g. ``PO32163``), the
three-way-match join key per ADR-0009, falling back to the internal order id.
"""

from __future__ import annotations

from dataclasses import dataclass

from purchase_order_builder import (
    Component,
    Fee,
    OrderItem,
    POLine,
    PurchaseOrder,
    ShipTo,
    build_purchase_order,
)

VENDOR_CODE = "CROWN"


class PurchaseOrderError(Exception):
    """Raised when an order can't be turned into a PO (missing order/vendor)."""


@dataclass
class OrderContext:
    """Everything the builder needs, resolved from the DB for one order."""
    shift4_order_id: int
    po_number: str
    vendor_id: int
    ship_to: ShipTo
    comments: str | None
    order_items: list[OrderItem]
    bom_map: dict[str, list[Component]]
    passthrough_prices: dict[str, Component]


@dataclass
class PurchaseOrderWriteResult:
    purchase_order_id: int
    regenerated: bool          # True => an existing PO for this number was replaced
    line_count: int
    unpriced_skus: list[str]

    def __repr__(self) -> str:
        verb = "regenerated" if self.regenerated else "created"
        tail = f" unpriced={self.unpriced_skus}" if self.unpriced_skus else ""
        return (f"<PurchaseOrderWriteResult {verb} id={self.purchase_order_id} "
                f"lines={self.line_count}{tail}>")


def _city_line(city: str | None, state: str | None, zip_: str | None) -> str | None:
    """Compose 'City, ST 12345', tolerant of missing parts."""
    left = (city or "").strip()
    right = " ".join(p for p in [(state or "").strip(), (zip_ or "").strip()] if p)
    if left and right:
        return f"{left}, {right}"
    return left or right or None


# --------------------------------------------------------------------------- #
# Fetch
# --------------------------------------------------------------------------- #
def fetch_order_context(conn, shift4_order_id: int) -> OrderContext:
    """Read the order and resolve BOM + pricing into builder inputs."""
    cur = conn.cursor()

    cur.execute(
        "SELECT vendor_id FROM lpg.vendors WHERE vendor_code = %s",
        (VENDOR_CODE,),
    )
    row = cur.fetchone()
    if row is None:
        raise PurchaseOrderError(f"Vendor '{VENDOR_CODE}' not found")
    vendor_id = row[0]

    cur.execute(
        """
        SELECT invoice_number,
               ship_to_first_name, ship_to_last_name, ship_to_company,
               ship_to_address, ship_to_city, ship_to_state, ship_to_zip,
               ship_to_phone, comments
        FROM lpg.v_orders_effective
        WHERE shift4_order_id = %s
        """,
        (shift4_order_id,),
    )
    o = cur.fetchone()
    if o is None:
        raise PurchaseOrderError(f"Order {shift4_order_id} not found")
    (invoice_number, st_first, st_last, st_company, st_addr,
     st_city, st_state, st_zip, st_phone, comments) = o

    po_number = (invoice_number or "").strip() or str(shift4_order_id)
    ship_to = ShipTo(
        name=" ".join(p for p in [st_first, st_last] if p) or "",
        company=st_company,
        street=st_addr,
        city_line=_city_line(st_city, st_state, st_zip),
        phone=st_phone,
    )

    cur.execute(
        """
        SELECT sku, quantity, description
        FROM shift4.order_items
        WHERE shift4_order_id = %s
        ORDER BY id
        """,
        (shift4_order_id,),
    )
    order_items = [
        OrderItem(sku=r[0], quantity=r[1], description=r[2])
        for r in cur.fetchall()
    ]
    skus = list({it.sku for it in order_items})

    # BOM map: only combo SKUs (the exception list) come back with rows.
    bom_map: dict[str, list[Component]] = {}
    if skus:
        cur.execute(
            """
            SELECT pc.product_sku, vs.vendor_sku_id, vs.vendor_sku_code,
                   vs.description, vs.unit_cost, pc.quantity, pc.sort_order
            FROM lpg.product_components pc
            JOIN lpg.vendor_skus vs ON vs.vendor_sku_id = pc.vendor_sku_id
            WHERE pc.product_sku = ANY(%s)
            ORDER BY pc.product_sku, pc.sort_order
            """,
            (skus,),
        )
        for (product_sku, vsid, vscode, desc, unit_cost, qty_per, sort_order) in cur.fetchall():
            bom_map.setdefault(product_sku, []).append(
                Component(
                    vendor_sku_id=vsid,
                    vendor_sku_code=vscode,
                    description=desc,
                    unit_cost=unit_cost,
                    quantity_per=qty_per,
                    sort_order=sort_order,
                )
            )

    # Passthrough prices: order SKUs not in the BOM map, resolved by their own code.
    passthrough_skus = [s for s in skus if s not in bom_map]
    passthrough_prices: dict[str, Component] = {}
    if passthrough_skus:
        cur.execute(
            """
            SELECT vendor_sku_id, vendor_sku_code, description, unit_cost
            FROM lpg.vendor_skus
            WHERE vendor_id = %s AND vendor_sku_code = ANY(%s)
            """,
            (vendor_id, passthrough_skus),
        )
        for (vsid, vscode, desc, unit_cost) in cur.fetchall():
            passthrough_prices[vscode] = Component(
                vendor_sku_id=vsid,
                vendor_sku_code=vscode,
                description=desc,
                unit_cost=unit_cost,
                quantity_per=1,
                sort_order=1,
            )

    return OrderContext(
        shift4_order_id=shift4_order_id,
        po_number=po_number,
        vendor_id=vendor_id,
        ship_to=ship_to,
        comments=comments,
        order_items=order_items,
        bom_map=bom_map,
        passthrough_prices=passthrough_prices,
    )


# --------------------------------------------------------------------------- #
# Persist
# --------------------------------------------------------------------------- #
def write_purchase_order(conn, po: PurchaseOrder) -> PurchaseOrderWriteResult:
    """Insert or replace the PO + its lines. Idempotent on po_number.

    Regeneration updates the header in place, resets status to 'draft', and
    replaces all lines. (Caller/endpoint is responsible for refusing to
    regenerate a PO that has already been sent, if that policy is wanted.)
    """
    cur = conn.cursor()

    cur.execute(
        "SELECT purchase_order_id FROM lpg.purchase_orders WHERE po_number = %s",
        (po.po_number,),
    )
    existing = cur.fetchone()

    if existing:
        purchase_order_id = existing[0]
        regenerated = True
        cur.execute(
            """
            UPDATE lpg.purchase_orders SET
                shift4_order_id = %s,
                vendor_id       = %s,
                status          = 'draft'::lpg.purchase_order_status,
                ship_name       = %s,
                ship_company    = %s,
                ship_street     = %s,
                ship_city_line  = %s,
                ship_phone      = %s,
                comments        = %s
            WHERE purchase_order_id = %s
            """,
            (po.shift4_order_id, po.vendor_id, po.ship_to.name, po.ship_to.company,
             po.ship_to.street, po.ship_to.city_line, po.ship_to.phone, po.comments,
             purchase_order_id),
        )
        cur.execute(
            "DELETE FROM lpg.purchase_order_lines WHERE purchase_order_id = %s",
            (purchase_order_id,),
        )
    else:
        regenerated = False
        cur.execute(
            """
            INSERT INTO lpg.purchase_orders (
                po_number, shift4_order_id, vendor_id, status,
                ship_name, ship_company, ship_street, ship_city_line, ship_phone,
                comments
            ) VALUES (
                %s, %s, %s, 'draft'::lpg.purchase_order_status,
                %s, %s, %s, %s, %s, %s
            )
            RETURNING purchase_order_id
            """,
            (po.po_number, po.shift4_order_id, po.vendor_id,
             po.ship_to.name, po.ship_to.company, po.ship_to.street,
             po.ship_to.city_line, po.ship_to.phone, po.comments),
        )
        purchase_order_id = cur.fetchone()[0]

    for line in po.lines:
        cur.execute(
            """
            INSERT INTO lpg.purchase_order_lines (
                purchase_order_id, is_fee, vendor_sku_id, vendor_sku_code,
                description, quantity, unit_cost, amount, sort_order
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (purchase_order_id, line.is_fee, line.vendor_sku_id, line.vendor_sku_code,
             line.description, line.quantity, line.unit_cost, line.amount,
             line.sort_order),
        )

    return PurchaseOrderWriteResult(
        purchase_order_id=purchase_order_id,
        regenerated=regenerated,
        line_count=len(po.lines),
        unpriced_skus=list(po.unpriced_skus),
    )


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #
def generate_purchase_order(
    conn,
    shift4_order_id: int,
    *,
    fees: list[Fee] | None = None,
) -> tuple[PurchaseOrder, PurchaseOrderWriteResult]:
    """Fetch -> build -> persist for one order. Returns the built PO and result."""
    ctx = fetch_order_context(conn, shift4_order_id)
    po = build_purchase_order(
        po_number=ctx.po_number,
        shift4_order_id=ctx.shift4_order_id,
        vendor_id=ctx.vendor_id,
        order_items=ctx.order_items,
        bom_map=ctx.bom_map,
        passthrough_prices=ctx.passthrough_prices,
        ship_to=ctx.ship_to,
        comments=ctx.comments,
        fees=fees,
    )
    result = write_purchase_order(conn, po)
    return po, result


# --------------------------------------------------------------------------- #
# Load (reconstruct a stored PO for rendering)
# --------------------------------------------------------------------------- #
def load_purchase_order(conn, po_number: str) -> PurchaseOrder:
    """Reconstruct a stored ``PurchaseOrder`` (header + lines) from the DB.

    Used to render a PDF of an already-generated PO without re-running the
    explosion. ``unpriced_skus`` is a generation-time artifact (not persisted),
    so it comes back empty — a stored PO only ever holds priced lines.
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT purchase_order_id, po_number, shift4_order_id, vendor_id,
               ship_name, ship_company, ship_street, ship_city_line, ship_phone,
               comments
        FROM lpg.purchase_orders
        WHERE po_number = %s
        """,
        (po_number,),
    )
    row = cur.fetchone()
    if row is None:
        raise PurchaseOrderError(f"Purchase order {po_number} not found")
    (purchase_order_id, po_num, shift4_order_id, vendor_id,
     ship_name, ship_company, ship_street, ship_city_line, ship_phone,
     comments) = row

    cur.execute(
        """
        SELECT is_fee, sort_order, vendor_sku_id, vendor_sku_code,
               description, quantity, unit_cost, amount
        FROM lpg.purchase_order_lines
        WHERE purchase_order_id = %s
        ORDER BY sort_order
        """,
        (purchase_order_id,),
    )
    lines = [
        POLine(
            is_fee=r[0],
            sort_order=r[1],
            vendor_sku_id=r[2],
            vendor_sku_code=r[3],
            description=r[4],
            quantity=r[5],
            unit_cost=r[6],
            amount=r[7],
        )
        for r in cur.fetchall()
    ]

    return PurchaseOrder(
        po_number=po_num,
        shift4_order_id=shift4_order_id,
        vendor_id=vendor_id,
        ship_to=ShipTo(
            name=ship_name or "",
            company=ship_company,
            street=ship_street,
            city_line=ship_city_line,
            phone=ship_phone,
        ),
        comments=comments,
        lines=lines,
        unpriced_skus=[],
    )


# --------------------------------------------------------------------------- #
# Send-flow helpers
# --------------------------------------------------------------------------- #
def get_vendor_po_email(conn, vendor_id: int) -> str | None:
    """The email POs are sent to for this vendor (lpg.vendors.po_email)."""
    cur = conn.cursor()
    cur.execute(
        "SELECT po_email FROM lpg.vendors WHERE vendor_id = %s",
        (vendor_id,),
    )
    row = cur.fetchone()
    return row[0] if row else None


def get_purchase_order_status(conn, po_number: str) -> str | None:
    """Current status of a PO ('draft'/'sent'), or None if it doesn't exist."""
    cur = conn.cursor()
    cur.execute(
        "SELECT status FROM lpg.purchase_orders WHERE po_number = %s",
        (po_number,),
    )
    row = cur.fetchone()
    return row[0] if row else None


def mark_purchase_order_sent(conn, po_number: str, *, pdf_uri: str | None = None) -> None:
    """Mark a PO as sent and stamp sent_at = now().

    If ``pdf_uri`` is given, record it as the archived sent-PDF location. A None
    uri (archival failed/skipped) leaves any existing value intact via COALESCE,
    so a storage hiccup never erases a prior archive reference.
    """
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE lpg.purchase_orders
        SET status = 'sent'::lpg.purchase_order_status,
            sent_at = now(),
            pdf_gcs_uri = COALESCE(%s, pdf_gcs_uri)
        WHERE po_number = %s
        """,
        (pdf_uri, po_number),
    )


# --------------------------------------------------------------------------- #
# Serialization (API response shape)
# --------------------------------------------------------------------------- #
def purchase_order_to_dict(
    po: PurchaseOrder,
    result: PurchaseOrderWriteResult,
) -> dict:
    """Serialize a built PO + write result into a JSON-safe dict for the API.

    Decimals are rendered as strings to avoid float rounding over the wire.
    """
    return {
        "po_number": po.po_number,
        "purchase_order_id": result.purchase_order_id,
        "shift4_order_id": po.shift4_order_id,
        "vendor_id": po.vendor_id,
        "status": "draft",
        "regenerated": result.regenerated,
        "ship_to": {
            "name": po.ship_to.name,
            "company": po.ship_to.company,
            "street": po.ship_to.street,
            "city_line": po.ship_to.city_line,
            "phone": po.ship_to.phone,
        },
        "comments": po.comments,
        "lines": [
            {
                "sort_order": ln.sort_order,
                "is_fee": ln.is_fee,
                "vendor_sku_code": ln.vendor_sku_code,
                "description": ln.description,
                "quantity": ln.quantity,
                "unit_cost": str(ln.unit_cost) if ln.unit_cost is not None else None,
                "amount": str(ln.amount) if ln.amount is not None else None,
            }
            for ln in po.lines
        ],
        "line_count": result.line_count,
        "unpriced_skus": list(result.unpriced_skus),
    }
