"""Seed lpg.vendor_skus from a Crown Plastics CSV.

Usage:
    python scripts/seed_crown_pricing.py data/crown-skus-2025-06-01.csv

Reads the CSV and upserts rows into lpg.vendor_skus using
vendor_sku_code as the conflict key. Reports how many rows
were inserted vs updated.

Reuses webhook-handler/db.py for the database connection so the
same IAM-vs-password logic applies (locally uses PGPASSWORD, on
Cloud Run would use IAM). Run via the Cloud SQL proxy on local dev.

The vendor row for Crown must already exist (we seeded it manually
yesterday). Look up its vendor_id at startup; abort if not found.
"""

from __future__ import annotations

import csv
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

from lpg_common.db import get_connection

VENDOR_CODE = "CROWN"


def _parse_decimal(value: str) -> Decimal | None:
    """Parse a CSV cell as Decimal, returning None for empty/invalid."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def _parse_int(value: str, default: int | None = None) -> int | None:
    """Parse a CSV cell as int, returning default for empty/invalid."""
    value = (value or "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def seed(csv_path: Path) -> dict:
    """Seed vendor_skus from the CSV. Returns stats dict."""
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV not found: {csv_path}")

    stats = {"inserted": 0, "updated": 0, "skipped": 0, "errors": 0}

    with get_connection() as conn:
        cur = conn.cursor()

        # Look up Crown's vendor_id.
        cur.execute(
            "SELECT vendor_id FROM lpg.vendors WHERE vendor_code = %s",
            (VENDOR_CODE,),
        )
        row = cur.fetchone()
        if row is None:
            raise RuntimeError(
                f"Vendor '{VENDOR_CODE}' not found in lpg.vendors. "
                f"Seed the vendor row first."
            )
        vendor_id = row[0]
        print(f"Found vendor '{VENDOR_CODE}' as vendor_id={vendor_id}")

        with csv_path.open("r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for line_num, row in enumerate(reader, start=2):
                sku = (row.get("vendor_sku_code") or "").strip()
                if not sku:
                    stats["skipped"] += 1
                    continue

                unit_cost = _parse_decimal(row.get("unit_cost", ""))
                std_pack_qty = _parse_int(row.get("std_pack_qty", ""), default=1)
                std_skid_qty = _parse_int(row.get("std_skid_qty", ""))
                status = (row.get("status") or "active").strip()
                notes = (row.get("notes") or "").strip() or None
                description = (row.get("category") or "").strip() or None

                try:
                    cur.execute(
                        """
                        INSERT INTO lpg.vendor_skus (
                            vendor_id, vendor_sku_code, description,
                            unit_cost, std_pack_qty, std_skid_qty,
                            status, notes
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (vendor_id, vendor_sku_code) DO UPDATE SET
                            description   = EXCLUDED.description,
                            unit_cost     = EXCLUDED.unit_cost,
                            std_pack_qty  = EXCLUDED.std_pack_qty,
                            std_skid_qty  = EXCLUDED.std_skid_qty,
                            status        = EXCLUDED.status,
                            notes         = EXCLUDED.notes,
                            updated_at    = NOW()
                        RETURNING (xmax = 0) AS inserted
                        """,
                        (
                            vendor_id, sku, description,
                            unit_cost, std_pack_qty, std_skid_qty,
                            status, notes,
                        ),
                    )
                    inserted = cur.fetchone()[0]
                    if inserted:
                        stats["inserted"] += 1
                    else:
                        stats["updated"] += 1
                except Exception as exc:
                    print(f"  ERROR line {line_num} ({sku}): {exc}")
                    stats["errors"] += 1
                    conn.rollback()
                    # Start a new transaction so subsequent rows can proceed.
                    cur = conn.cursor()

        cur.close()

    return stats


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python scripts/seed_crown_pricing.py <csv-path>")
        return 1

    csv_path = Path(sys.argv[1])
    print(f"Seeding from: {csv_path}")
    stats = seed(csv_path)

    print()
    print("=== Summary ===")
    print(f"  Inserted: {stats['inserted']}")
    print(f"  Updated:  {stats['updated']}")
    print(f"  Skipped:  {stats['skipped']}")
    print(f"  Errors:   {stats['errors']}")

    return 0 if stats["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())