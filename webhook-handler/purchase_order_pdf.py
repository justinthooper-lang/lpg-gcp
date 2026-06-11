"""Render a PurchaseOrder to a PDF (ADR-0018, build step 3).

Pure: takes a ``PurchaseOrder`` (the builder's output) and returns PDF bytes. No
DB, no I/O target — the caller decides where the bytes go (GCS, a download, a file),
which keeps this unit-testable and matches the parser/writer purity ethos.

Library: reportlab (Platypus). Chosen over WeasyPrint because it is pure-Python with
no native system dependencies — a clean fit for the Cloud Run image (ADR-0018 Q4).

Layout reproduces the PO32163 field set from ADR-0018's PDF data contract: PO number
+ date header, a ship-to block (the dropship end customer), a line-items table
(Product ID | Description | Qty | Unit Cost | Amount), fees as sparse line items
(e.g. "Order Fee  15.00"), and a grand total.
"""

from __future__ import annotations

import io
from datetime import date
from decimal import Decimal

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

from purchase_order_builder import PurchaseOrder

SELLER_NAME = "Lamp Post Globes"
VENDOR_NAME = "Crown Plastics"


def _money(value: Decimal | None) -> str:
    return f"${value:,.2f}" if value is not None else ""


def _po_total(po: PurchaseOrder) -> Decimal:
    total = Decimal("0.00")
    for ln in po.lines:
        if ln.is_fee:
            total += ln.amount or Decimal("0")
        else:
            if ln.quantity is not None and ln.unit_cost is not None:
                total += Decimal(ln.quantity) * ln.unit_cost
    return total


def render_purchase_order_pdf(
    po: PurchaseOrder,
    *,
    seller_name: str = SELLER_NAME,
    vendor_name: str = VENDOR_NAME,
    doc_date: date | None = None,
) -> bytes:
    """Render ``po`` to PDF and return the bytes."""
    doc_date = doc_date or date.today()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=letter,
        topMargin=0.75 * inch,
        bottomMargin=0.75 * inch,
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
        title=f"Purchase Order {po.po_number}",
    )

    styles = getSampleStyleSheet()
    h_seller = ParagraphStyle("seller", parent=styles["Title"], fontSize=18,
                              alignment=0, spaceAfter=2)
    label = ParagraphStyle("label", parent=styles["Normal"], fontSize=9,
                           textColor=colors.HexColor("#666666"), spaceAfter=1)
    normal = styles["Normal"]
    right = ParagraphStyle("right", parent=styles["Normal"], alignment=2)

    story = []

    # --- Header band: seller (left) / PO number + date (right) ---
    header = Table(
        [[
            Paragraph(seller_name, h_seller),
            Paragraph(
                f"<b>PURCHASE ORDER</b><br/>"
                f"PO Number: <b>{po.po_number}</b><br/>"
                f"Date: {doc_date.isoformat()}",
                right,
            ),
        ]],
        colWidths=[3.5 * inch, 3.5 * inch],
    )
    header.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(header)
    story.append(Spacer(1, 0.3 * inch))

    # --- Vendor + Ship-to blocks ---
    s = po.ship_to
    ship_lines = [v for v in [
        s.name, s.company, s.street, s.city_line, s.phone
    ] if v]
    ship_block = "<br/>".join(ship_lines) if ship_lines else "<i>(no ship-to on order)</i>"

    addr = Table(
        [[
            Paragraph("VENDOR", label),
            Paragraph("SHIP TO", label),
        ],
         [
            Paragraph(vendor_name, normal),
            Paragraph(ship_block, normal),
        ]],
        colWidths=[3.5 * inch, 3.5 * inch],
    )
    addr.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 1), (-1, 1), 2),
    ]))
    story.append(addr)
    story.append(Spacer(1, 0.3 * inch))

    # --- Line items ---
    cell = ParagraphStyle("cell", parent=normal, fontSize=9, leading=11)
    header_row = ["Product ID", "Description", "Qty", "Unit Cost", "Amount"]
    rows = [header_row]
    for ln in po.lines:
        if ln.is_fee:
            # Sparse fee line: label under Description, amount in Amount col.
            rows.append(["", Paragraph(ln.description or "Fee", cell), "", "",
                         _money(ln.amount)])
        else:
            ext = (Decimal(ln.quantity) * ln.unit_cost
                   if ln.quantity is not None and ln.unit_cost is not None else None)
            rows.append([
                Paragraph(ln.vendor_sku_code or "", cell),   # joined SKUs wrap here
                Paragraph(ln.description or "", cell),
                str(ln.quantity) if ln.quantity is not None else "",
                _money(ln.unit_cost),
                _money(ext),
            ])
    rows.append(["", "", "", "Total", _money(_po_total(po))])

    table = Table(
        rows,
        colWidths=[1.7 * inch, 2.5 * inch, 0.5 * inch, 1.0 * inch, 1.0 * inch],
        repeatRows=1,
    )
    last = len(rows) - 1
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#222222")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ALIGN", (2, 1), (4, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ROWBACKGROUNDS", (0, 1), (-1, last - 1),
         [colors.white, colors.HexColor("#f4f4f4")]),
        ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.black),
        ("LINEABOVE", (0, last), (-1, last), 0.5, colors.black),
        ("FONTNAME", (3, last), (4, last), "Helvetica-Bold"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(table)

    # --- Comments ---
    if po.comments:
        story.append(Spacer(1, 0.25 * inch))
        story.append(Paragraph("Comments", label))
        story.append(Paragraph(po.comments, normal))

    if po.unpriced_skus:
        story.append(Spacer(1, 0.2 * inch))
        story.append(Paragraph(
            "Unpriced SKUs (omitted — no vendor price): "
            + ", ".join(po.unpriced_skus),
            ParagraphStyle("warn", parent=normal, textColor=colors.red, fontSize=8),
        ))

    doc.build(story)
    return buf.getvalue()
