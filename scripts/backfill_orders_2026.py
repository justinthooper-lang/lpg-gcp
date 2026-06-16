#!/usr/bin/env python3
"""One-time backfill of 2026 Shift4Shop orders into shift4.* (ADR-0009 contract).

The webhook only ingests orders going forward; this loads history. It reuses the
EXACT ingest path (``ingest_order``) and payload contract (``Shift4OrderPayload``)
from webhook-handler, so combo explosion, customer/product stubbing, shipment
rows, and dedup all come for free — and re-running is safe because every write is
an idempotent upsert (orders dedup on shift4_order_id).

Status filter mirrors the webhook exactly (decision 2026-06-16): only
New(1) / Processing(2) / Shipped(4) are ingested; Quote(21) and every other
status are skipped. (The DB also rejects Quote via CHECK as a backstop.)

Sources:
  (default)                  page the Shift4Shop REST API GET /Orders
  --source-file orders.json  read a JSON array of Shift4 order objects (offline)

Credentials come from the environment — never hard-coded or committed:
  SHIFT4_SECURE_URL, SHIFT4_PRIVATE_KEY, SHIFT4_TOKEN   (REST API auth headers)
  SHIFT4_API_BASE   optional; default https://apirest.3dcart.com/3dCartWebAPI/v1
DB access uses lpg_common.get_connection — set PGPASSWORD for local password auth,
like the other local tooling (architecture.md).

Usage:
  # safe first pass — fetch + classify, NO writes:
  python scripts/backfill_orders_2026.py --dry-run
  # real load of the full 2026 window:
  python scripts/backfill_orders_2026.py
  # a small live slice to sanity-check first:
  python scripts/backfill_orders_2026.py --limit 5
  # custom window / offline file:
  python scripts/backfill_orders_2026.py --start 01/01/2026 --end 06/16/2026
  python scripts/backfill_orders_2026.py --source-file export.json --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date

# Reuse the webhook-handler's contract + ingest path verbatim.
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "webhook-handler"))

from shift4_models import ORDER_STATUS_MAP, Shift4OrderPayload  # noqa: E402
from ingest import ingest_order  # noqa: E402

API_BASE = os.getenv("SHIFT4_API_BASE", "https://apirest.3dcart.com/3dCartWebAPI/v1")
PAGE_SIZE = 100


def _api_headers() -> dict:
    missing = [v for v in ("SHIFT4_SECURE_URL", "SHIFT4_PRIVATE_KEY", "SHIFT4_TOKEN")
               if not os.getenv(v)]
    if missing:
        sys.exit(f"Missing required API env vars: {', '.join(missing)}")
    return {
        "SecureUrl": os.environ["SHIFT4_SECURE_URL"],
        "PrivateKey": os.environ["SHIFT4_PRIVATE_KEY"],
        "Token": os.environ["SHIFT4_TOKEN"],
        "Accept": "application/json",
    }


def fetch_api_page(headers: dict, start: str, end: str, offset: int) -> list:
    """One page of GET /Orders. Returns a list (empty when no more orders)."""
    qs = urllib.parse.urlencode({
        "limit": PAGE_SIZE, "offset": offset,
        "datestart": start, "dateend": end,
    })
    url = f"{API_BASE}/Orders?{qs}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:  # Shift4Shop returns 404 when the window has no orders
            return []
        body = e.read().decode("utf-8", "replace")[:300]
        sys.exit(f"API error {e.code} at offset {offset}: {body}")


def iter_api_orders(start: str, end: str):
    headers = _api_headers()
    offset = 0
    while True:
        page = fetch_api_page(headers, start, end, offset)
        if not page:
            return
        yield from page
        if len(page) < PAGE_SIZE:
            return
        offset += PAGE_SIZE


def iter_file_orders(path: str):
    with open(path) as fh:
        data = json.load(fh)
    if isinstance(data, dict):
        data = [data]
    yield from data


def classify(order: dict):
    """Return (payload, action) where action is 'ingest' | 'skip:quote' | 'skip:status'."""
    payload = Shift4OrderPayload.model_validate(order)
    status_text = ORDER_STATUS_MAP.get(payload.order_status_id)
    if status_text is None:
        return payload, "skip:status"
    if status_text == "Quote":
        return payload, "skip:quote"
    return payload, "ingest"


def main() -> None:
    ap = argparse.ArgumentParser(description="One-time 2026 Shift4 order backfill.")
    ap.add_argument("--start", default="01/01/2026", help="datestart MM/DD/YYYY")
    ap.add_argument("--end", default=date.today().strftime("%m/%d/%Y"),
                    help="dateend MM/DD/YYYY (default: today)")
    ap.add_argument("--source-file", help="read orders from a JSON file instead of the API")
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch + classify only; perform NO database writes")
    ap.add_argument("--limit", type=int, default=0,
                    help="stop after N orders (0 = all) — handy for a test slice")
    args = ap.parse_args()

    if args.source_file:
        source = iter_file_orders(args.source_file)
        print(f"Source: file {args.source_file}")
    else:
        source = iter_api_orders(args.start, args.end)
        print(f"Source: Shift4Shop API {API_BASE}  window {args.start} .. {args.end}")
    print(f"Mode:   {'DRY-RUN (no writes)' if args.dry_run else 'LIVE ingest'}\n")

    counts = {"fetched": 0, "ingested": 0, "skip_status": 0, "skip_quote": 0, "error": 0}
    for order in source:
        counts["fetched"] += 1
        oid = order.get("OrderID")
        try:
            payload, action = classify(order)
        except Exception as exc:  # malformed order — record and keep going
            counts["error"] += 1
            print(f"  ! order {oid}: parse error: {exc}")
            continue

        if action == "ingest":
            if args.dry_run:
                counts["ingested"] += 1
            else:
                try:
                    ingest_order(payload)
                    counts["ingested"] += 1
                except Exception as exc:
                    counts["error"] += 1
                    print(f"  ! order {oid}: ingest error: {exc}")
                    continue
        elif action == "skip:quote":
            counts["skip_quote"] += 1
        else:
            counts["skip_status"] += 1

        if counts["fetched"] % 50 == 0:
            print(f"  ... {counts['fetched']} fetched, {counts['ingested']} ingested")
        if args.limit and counts["fetched"] >= args.limit:
            print(f"  (stopped at --limit {args.limit})")
            break

    print("\n=== Backfill summary ===")
    for k in ("fetched", "ingested", "skip_status", "skip_quote", "error"):
        print(f"  {k:12} {counts[k]}")
    if counts["error"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
