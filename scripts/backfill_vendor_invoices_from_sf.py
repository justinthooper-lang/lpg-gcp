"""One-time backfill of lpg.vendor_invoices from a Salesforce margin export.

LPG's prior system (Salesforce) recorded, per order, the *actual* Crown supplier
cost and freight. This script migrates those recorded actuals into
lpg.vendor_invoices so historical orders gain a true cost/freight basis without
generating purchase orders — the order<->invoice match is direct on
orders.invoice_number = vendor_invoices.customer_po_number (the PO# Crown prints
as the customer PO).

Provenance is explicit so migrated rows never masquerade as Crown-PDF-ingested
ones:
  - graph_message_id   = 'sf-migration:<PO>'   (satisfies the NOT NULL; marks source)
  - vendor_invoice_number = 'sf:<PO>'          (unique per (vendor_id, number); idempotent)
These are *backstop* rows. If/when the real Crown invoice for the same PO is
ingested via the daily sync (with its true Crown invoice number, e.g. 228031),
it lands as a separate row; the margin view prefers the real Crown row over the
'sf:' backstop. (See lpg.v_order_margins.)

Only rows with a real supplier cost are loaded. Rows without a Supplier Invoice
Total (un-invoiced drafts) are skipped — they must NOT get a fabricated invoice,
or they'd show an unrealistic ~100%% margin.

Freight: the export carries one combined "Supplier Actual Shipping" figure. Each
order ships truck XOR UPS, so only one freight column is ever meaningful; the
combined value goes into freight_truck as a catch-all (the margin view sums
truck+ups, so the total is correct regardless of column). freight_type is left
NULL for migrated rows since the export doesn't say which method.

CSV columns used:
  Shift4Shop Order Number  -> customer_po_number   (e.g. PO31938)
  Supplier Invoice Total   -> sale_amount          (Crown product cost)
  Supplier Actual Shipping -> freight_truck        (combined actual freight)
  Order Start Date         -> invoice_date         (best available date)

Usage:
  PGPASSWORD=... python scripts/backfill_vendor_invoices_from_sf.py FILE.csv --dry-run
  PGPASSWORD=... python scripts/backfill_vendor_invoices_from_sf.py FILE.csv --limit 5
  PGPASSWORD=... python scripts/backfill_vendor_invoices_from_sf.py FILE.csv
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from datetime import datetime
from decimal import Decimal, InvalidOperation

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "webhook-handler"))

from lpg_common.db import get_connection  # noqa: E402

VENDOR_CODE = "crown"


def _money(s: str | None) -> Decimal | None:
    s = (s or "").strip().replace(",", "").replace("$", "")
    if s in ("", "-"):
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def _date(s: str | None):
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def load_rows(csv_path: str) -> list[dict]:
    with open(csv_path, newline="") as f:
        return list(csv.DictReader(f))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("csv_path", help="Salesforce margin export CSV")
    ap.add_argument("--dry-run", action="store_true", help="classify only; no writes")
    ap.add_argument("--limit", type=int, default=None, help="stop after N loadable rows")
    args = ap.parse_args()

    rows = load_rows(args.csv_path)
    print(f"Source: {args.csv_path}  ({len(rows)} rows)")
    print(f"Mode:   {'DRY-RUN (no writes)' if args.dry_run else 'LIVE'}")

    loaded = skipped_no_cost = skipped_no_po = 0
    conflicts = 0

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT vendor_id FROM lpg.vendors WHERE UPPER(vendor_code) = UPPER(%s)", (VENDOR_CODE,))
        vrow = cur.fetchone()
        if vrow is None:
            sys.exit(f"Vendor '{VENDOR_CODE}' not found")
        vendor_id = vrow[0]

        for r in rows:
            po = (r.get("Shift4Shop Order Number") or "").strip()
            cost = _money(r.get("Supplier Invoice Total"))
            freight = _money(r.get("Supplier Actual Shipping"))
            inv_date = _date(r.get("Order Start Date"))

            if not po:
                skipped_no_po += 1
                continue
            if cost is None:
                skipped_no_cost += 1   # un-invoiced (draft); no fabricated cost
                continue

            if args.dry_run:
                loaded += 1
            else:
                cur.execute(
                    """
                    INSERT INTO lpg.vendor_invoices (
                        vendor_id, vendor_invoice_number, customer_po_number,
                        invoice_date, freight_type, freight_truck, freight_ups,
                        sale_amount, graph_message_id
                    )
                    VALUES (%s, %s, %s, %s, NULL, %s, NULL, %s, %s)
                    ON CONFLICT ON CONSTRAINT uq_vendor_invoice_number DO NOTHING
                    RETURNING vendor_invoice_id
                    """,
                    (
                        vendor_id,
                        f"sf:{po}",
                        po,
                        inv_date,
                        freight,
                        cost,
                        f"sf-migration:{po}",
                    ),
                )
                if cur.fetchone() is None:
                    conflicts += 1          # already loaded; idempotent
                else:
                    loaded += 1

            if args.limit and loaded >= args.limit:
                break

        if not args.dry_run:
            conn.commit()

    print("\n=== Backfill summary ===")
    print(f"  loaded            {loaded}")
    print(f"  already-present   {conflicts}")
    print(f"  skip_no_cost      {skipped_no_cost}  (un-invoiced drafts; correctly excluded)")
    print(f"  skip_no_po        {skipped_no_po}")


if __name__ == "__main__":
    main()
