"""Parse Crown Plastics PDF invoices into structured data.

Pure logic: bytes in, dict out. No Graph, no DB — so it's unit-testable
in isolation and reusable. The sync job imports `parse_crown_invoice`.

Crown invoices are a fixed Crystal Reports template, so layout-aware text
extraction (pdfplumber layout=True) is stable across invoices. Two guards
protect the money pipeline:
  1. Document-type guard — refuses Order Confirmations (a near-identical
     pre-ship document Crown emails on PO submit; it has projected costs
     and no real freight, so ingesting one would silently corrupt profit).
  2. Reconciliation guard — refuses invoices whose amounts don't add up,
     turning silent format drift into a loud failure.

See ADR-0016 (sync architecture) / ADR-0017 (scope lockdown).
"""

from __future__ import annotations

import io
import re
from decimal import Decimal

import pdfplumber


class NotAnInvoiceError(ValueError):
    """PDF is not a Crown invoice (e.g. an Order Confirmation)."""


class InvoiceReconcileError(ValueError):
    """Extracted amounts don't reconcile — signals parser/format drift."""


_MONEY = r"[-+]?\d[\d,]*\.\d{2}"
# Line items whose "item number" is actually a fee, not a product SKU.
FEE_ITEM_NOS = {"MIN ORDER FEE", "BKN CTN FEE"}


def _money(s: str | None) -> Decimal | None:
    return Decimal(s.replace(",", "")) if s else None


def _find(pattern: str, text: str, group: int = 1, flags: int = 0) -> str | None:
    m = re.search(pattern, text, flags)
    return m.group(group).strip() if m else None


def parse_crown_invoice(pdf_bytes: bytes) -> dict:
    """Parse Crown invoice PDF bytes into a structured dict.

    Raises NotAnInvoiceError if the PDF isn't an invoice, or
    InvoiceReconcileError if the extracted amounts don't add up.
    """
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        title = (pdf.metadata or {}).get("Title", "") or ""
        text = "\n".join(p.extract_text(layout=True) or "" for p in pdf.pages)

    # --- GUARD 1: document type ------------------------------------------
    if "Order Confirmation" in text or "Acknowledgement" in title:
        raise NotAnInvoiceError(f"Not an invoice (title={title!r})")
    if "Order Invoice" not in text:
        raise NotAnInvoiceError(f"Missing 'Order Invoice' header (title={title!r})")
    for required in ("Balance Due:", "Freight (UPS):", "Invoice Date"):
        if required not in text:
            raise NotAnInvoiceError(f"Missing required invoice field {required!r}")

    invoice = {
        "invoice_number":     _find(r"Order Invoice\s+(\d+)", text),
        "customer_po_number": _find(r"\b(PO\d+)\b", text),
        "order_no":           _find(r"(\d{6})\s+\d{1,2}/\d{1,2}/\d{4}", text),
        "invoice_date":       _find(r"\d{6}\s+(\d{1,2}/\d{1,2}/\d{4})", text),
        "customer_no":        _find(r"^\s*(\d{4})\s+Net\b", text, flags=re.M),
        "payment_terms":      _find(r"\b(Net \d+ Days?)\b", text),
        "ship_via":           _find(r"Net \d+ Days?\s+\d{1,2}/\d{1,2}/\d{4}\s+(.+?)\s{2,}\d", text),
        "tracking_number":    _find(r"Tracking #:\s*(\d+)", text),
        "sale_amount":        _money(_find(rf"Sale Amount:\s+({_MONEY})", text)),
        "freight_truck":      _money(_find(rf"Freight \(TRUCK\):\s+({_MONEY})", text)),
        "freight_ups":        _money(_find(rf"Freight \(UPS\):\s+({_MONEY})", text)),
        # Crown labels the grand total "SubTotal" (counterintuitive).
        "grand_total":        _money(_find(rf"SubTotal:\s+({_MONEY})", text)),
        "amount_received":    _money(_find(rf"Amount Received:\s+({_MONEY})", text)),
        "balance_due":        _money(_find(rf"Balance Due:\s+({_MONEY})", text)),
    }

    # row: L/I  item-no  qty-shipped  qty-bo  UOM  unit-price(4dp)  extended(2dp)
    row_re = re.compile(
        rf"^\s*(\d+)\s+(.+?)\s+(\d+)\s+(\d+)\s+([A-Z]{{2}})\s+(\d+\.\d{{4}})\s+({_MONEY})\s*$",
        re.M,
    )
    line_items = [
        {
            "line_no":        int(m.group(1)),
            "item_no":        m.group(2).strip(),
            "qty_shipped":    int(m.group(3)),
            "qty_bo":         int(m.group(4)),
            "uom":            m.group(5),
            "unit_price":     _money(m.group(6)),
            "extended_price": _money(m.group(7)),
            "is_fee":         m.group(2).strip().upper() in FEE_ITEM_NOS,
        }
        for m in row_re.finditer(text)
    ]
    invoice["line_items"] = line_items

    # --- GUARD 2: amounts must reconcile ---------------------------------
    line_sum = sum((it["extended_price"] for it in line_items), Decimal("0"))
    if invoice["sale_amount"] is None or line_sum != invoice["sale_amount"]:
        raise InvoiceReconcileError(
            f"line items {line_sum} != Sale Amount {invoice['sale_amount']}"
        )
    freight = (invoice["freight_truck"] or Decimal("0")) + (invoice["freight_ups"] or Decimal("0"))
    if invoice["sale_amount"] + freight != invoice["grand_total"]:
        raise InvoiceReconcileError(
            f"sale {invoice['sale_amount']} + freight {freight} != total {invoice['grand_total']}"
        )

    return invoice


if __name__ == "__main__":
    # Local test: python3 scripts/crown_invoice_parser.py path/to/invoice.pdf
    import json
    import sys

    with open(sys.argv[1], "rb") as f:
        result = parse_crown_invoice(f.read())
    items = result.pop("line_items")
    print(json.dumps({k: str(v) for k, v in result.items()}, indent=2))
    for it in items:
        print("  ", {k: str(v) for k, v in it.items()})