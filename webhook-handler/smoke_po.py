"""Manual smoke test / CLI for purchase-order generation (ADR-0018).

Dry run (default) — fetch + build + print, NO write:
    python smoke_po.py 301748

Persist the draft PO (insert/replace rows):
    python smoke_po.py 301748 --write

Optionally attach manual fees (ADR-0018 Q2 = manual):
    python smoke_po.py 301748 --order-fee 15 --broken-carton-fee 15 --write

Uses lpg_common.db.get_connection(), so it talks to whatever the environment
points at (local Cloud SQL Auth Proxy for dev). Dry run rolls back; --write commits.
"""

from __future__ import annotations

import argparse
from decimal import Decimal

from lpg_common.db import get_connection
from purchase_order_builder import Fee, build_purchase_order
from purchase_order_repository import (
    fetch_order_context,
    write_purchase_order,
)


def _print_po(po) -> None:
    print(f"  PO number : {po.po_number}")
    print(f"  Vendor id : {po.vendor_id}")
    print(f"  Ship to   : {po.ship_to.name} | {po.ship_to.company or ''} | "
          f"{po.ship_to.street or ''} | {po.ship_to.city_line or ''} | "
          f"{po.ship_to.phone or ''}")
    print(f"  Comments  : {po.comments or ''}")
    print(f"  Lines     : {len(po.lines)}")
    for ln in po.lines:
        if ln.is_fee:
            print(f"    [{ln.sort_order}] FEE  {ln.description:<22} "
                  f"amount={ln.amount}")
        else:
            print(f"    [{ln.sort_order}] ITEM {ln.vendor_sku_code:<16} "
                  f"qty={ln.quantity} unit_cost={ln.unit_cost} "
                  f"({ln.description or ''})")
    if po.unpriced_skus:
        print(f"  UNPRICED  : {po.unpriced_skus}  <-- could not price; "
              f"not emitted as lines")


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate a Crown PO for an order.")
    ap.add_argument("shift4_order_id", type=int)
    ap.add_argument("--write", action="store_true",
                    help="Persist the PO (default is a dry run that rolls back).")
    ap.add_argument("--order-fee", type=Decimal, default=None)
    ap.add_argument("--broken-carton-fee", type=Decimal, default=None)
    args = ap.parse_args()

    fees: list[Fee] = []
    if args.order_fee is not None:
        fees.append(Fee("Order Fee", args.order_fee))
    if args.broken_carton_fee is not None:
        fees.append(Fee("Broken Carton Fee", args.broken_carton_fee))

    with get_connection() as conn:
        ctx = fetch_order_context(conn, args.shift4_order_id)
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

        mode = "WRITE" if args.write else "DRY RUN"
        print(f"=== PO generation ({mode}) for order {args.shift4_order_id} ===")
        _print_po(po)

        if args.write:
            result = write_purchase_order(conn, po)
            print(f"\n  {result}")
            # get_connection() commits on clean exit.
        else:
            conn.rollback()
            print("\n  (dry run — nothing written)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
