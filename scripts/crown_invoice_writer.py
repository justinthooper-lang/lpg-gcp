"""Persist a parsed Crown invoice into lpg.vendor_invoices / _lines.

Persistence layer only. Takes an OPEN pg8000 connection plus the parsed
dict (from crown_invoice_parser.parse_crown_invoice) and the email-side
context the PDF can't supply: vendor_id and graph_message_id. The caller
owns the transaction — wrap this in webhook-handler's get_connection()
so the header and all lines commit atomically, or roll back together.

Re-sync is idempotent on graph_message_id: an already-captured invoice is
skipped (invoices are immutable point-in-time documents; Crown reissues
corrections under a new invoice number, i.e. a new row). See ADR-0016.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any


class CrownInvoiceWriteResult:
    """Outcome of a write attempt."""

    def __init__(self, inserted: bool, vendor_invoice_id: int | None, line_count: int):
        self.inserted = inserted              # False => skipped (already present)
        self.vendor_invoice_id = vendor_invoice_id
        self.line_count = line_count

    def __repr__(self) -> str:
        verb = "inserted" if self.inserted else "skipped (already present)"
        return (f"<CrownInvoiceWriteResult {verb} "
                f"id={self.vendor_invoice_id} lines={self.line_count}>")


def _parse_date(s: str | None) -> date | None:
    """Crown prints dates as M/D/YYYY (e.g. 6/8/2026)."""
    if not s:
        return None
    return datetime.strptime(s, "%m/%d/%Y").date()


def _freight_type(invoice: dict[str, Any]) -> str | None:
    """Derive freight_type from whichever freight line is non-zero.

    Matches the chk_vendor_invoices_freight_type CHECK ('ups'|'truck'|NULL).
    """
    ups = invoice.get("freight_ups") or Decimal("0")
    truck = invoice.get("freight_truck") or Decimal("0")
    if ups > 0:
        return "ups"
    if truck > 0:
        return "truck"
    return None


def write_crown_invoice(
    conn,
    invoice: dict[str, Any],
    *,
    vendor_id: int,
    graph_message_id: str,
    raw_pdf_filename: str | None = None,
) -> CrownInvoiceWriteResult:
    """Insert one parsed invoice + its lines. Skips if already captured.

    The caller's context manager handles commit/rollback.
    """
    tracking = invoice.get("tracking_number")
    tracking_numbers = [tracking] if tracking else None

    cur = conn.cursor()

    # Header. Dedup on (vendor_id, vendor_invoice_number) — NOT graph_message_id.
    # Crown sends two identical emails per invoice (a vendor-side quirk), so the
    # copies have different message IDs but the same invoice number. The invoice
    # number is the true business identity, so it's the right idempotency key:
    # it skips both Crown's second copy and any genuine re-sync. First email to
    # arrive wins; since the copies are identical, which one is irrelevant.
    # RETURNING yields a row only on insert, so a None return == skipped.
    cur.execute(
        """
        INSERT INTO lpg.vendor_invoices (
            vendor_id, vendor_invoice_number, vendor_order_number,
            customer_po_number, invoice_date, ship_via,
            tracking_numbers, freight_type, freight_truck, freight_ups,
            subtotal, sale_amount, amount_received, balance_due,
            raw_pdf_filename, graph_message_id
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
        )
        ON CONFLICT ON CONSTRAINT uq_vendor_invoice_number DO NOTHING
        RETURNING vendor_invoice_id
        """,
        (
            vendor_id,
            invoice["invoice_number"],
            invoice.get("order_no"),
            invoice.get("customer_po_number"),
            _parse_date(invoice.get("invoice_date")),
            invoice.get("ship_via"),
            tracking_numbers,
            _freight_type(invoice),
            invoice.get("freight_truck"),
            invoice.get("freight_ups"),
            invoice.get("grand_total"),     # Crown's "SubTotal" => subtotal column
            invoice.get("sale_amount"),
            invoice.get("amount_received"),
            invoice.get("balance_due"),
            raw_pdf_filename,
            graph_message_id,
        ),
    )
    row = cur.fetchone()
    if row is None:
        return CrownInvoiceWriteResult(inserted=False, vendor_invoice_id=None, line_count=0)

    vendor_invoice_id = row[0]

    lines = invoice.get("line_items", [])
    for li in lines:
        cur.execute(
            """
            INSERT INTO lpg.vendor_invoice_lines (
                vendor_invoice_id, line_number, vendor_sku_code,
                qty_shipped, qty_backorder, uom,
                unit_price, extended_price, is_fee
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                vendor_invoice_id,
                li["line_no"],
                li["item_no"],
                li["qty_shipped"],
                li["qty_bo"],
                li.get("uom"),
                li.get("unit_price"),
                li.get("extended_price"),
                li["is_fee"],
            ),
        )

    return CrownInvoiceWriteResult(
        inserted=True, vendor_invoice_id=vendor_invoice_id, line_count=len(lines)
    )


if __name__ == "__main__":
    # Local end-to-end test: parse a real PDF, then write it.
    #   python3 scripts/crown_invoice_writer.py <pdf_path> <graph_message_id>
    # Requires the Cloud SQL Auth Proxy running and PGPASSWORD set.
    import sys

    from crown_invoice_parser import parse_crown_invoice

    pdf_path = sys.argv[1]
    msg_id = sys.argv[2] if len(sys.argv) > 2 else "TEST-LOCAL-0001"

    with open(pdf_path, "rb") as f:
        parsed = parse_crown_invoice(f.read())

    sys.path.insert(0, "webhook-handler")
    from db import get_connection

    with get_connection() as conn:
        result = write_crown_invoice(
            conn,
            parsed,
            vendor_id=1,                       # Crown
            graph_message_id=msg_id,
            raw_pdf_filename=pdf_path.split("/")[-1],
        )
    print(result)