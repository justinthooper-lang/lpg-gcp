"""Seed lpg.product_components from a kit BOM CSV.

Usage:
    python scripts/seed_kit_boms.py data/kit-boms-2025-06-01.csv

CSV format (from the previous Salesforce project):
    id,converted id,cost
    20012-WH-6F,20012-WH-XX/98006-P,21.40

Each row describes a storefront SKU ("id") that's assembled from
N Crown SKUs joined by '/' ("converted id"). The "cost" column is
ignored — it was a denormalized total in the old design. We rely
on SUM(vendor_skus.unit_cost * pc.quantity) at query time, so cost
is always live with respect to current Crown pricing.

For each row, the script inserts one lpg.product_components row per
component, with quantity=1 (kits are 1-of-each by convention) and
sort_order matching the order in the '/' string (so the order is
stable across re-runs and reflects the original kit definition).

Idempotency: if a storefront SKU already has any product_components
rows, the script SKIPS it. Don't double-map. To re-seed a specific
SKU, manually DELETE its existing rows first.

Component lookup: looks up vendor_sku_id by vendor_sku_code under
Crown's vendor_id. If a component SKU is missing from vendor_skus,
the kit is SKIPPED with a warning — never auto-created.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "webhook-handler"))

from db import get_connection  # noqa: E402

VENDOR_CODE = "CROWN"


def parse_components(converted_id: str) -> list[str]:
    """Split '20012-WH-XX/98006-P' into ['20012-WH-XX', '98006-P']."""
    return [s.strip() for s in converted_id.split("/") if s.strip()]


def seed(csv_path: Path) -> dict:
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV not found: {csv_path}")

    stats = {
        "inserted": 0,
        "skipped_existing": 0,
        "skipped_missing_component": 0,
        "errors": 0,
    }

    with get_connection() as conn:
        cur = conn.cursor()

        # Resolve Crown's vendor_id.
        cur.execute(
            "SELECT vendor_id FROM lpg.vendors WHERE vendor_code = %s",
            (VENDOR_CODE,),
        )
        row = cur.fetchone()
        if row is None:
            raise RuntimeError(f"Vendor '{VENDOR_CODE}' not found")
        vendor_id = row[0]
        print(f"Vendor '{VENDOR_CODE}' = vendor_id {vendor_id}")

        with csv_path.open("r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for line_num, row in enumerate(reader, start=2):
                storefront_sku = (row.get("id") or "").strip()
                converted_id = (row.get("converted id") or "").strip()

                if not storefront_sku or not converted_id:
                    continue

                components = parse_components(converted_id)
                if not components:
                    print(f"  WARN line {line_num} ({storefront_sku}): "
                          f"no components parsed from '{converted_id}'")
                    continue

                # Skip if storefront SKU already has any mappings.
                cur.execute(
                    "SELECT COUNT(*) FROM lpg.product_components "
                    "WHERE product_sku = %s",
                    (storefront_sku,),
                )
                existing_count = cur.fetchone()[0]
                if existing_count > 0:
                    print(f"  SKIP {storefront_sku}: "
                          f"already has {existing_count} component(s)")
                    stats["skipped_existing"] += 1
                    continue

                # Resolve all components first; if any are missing, skip
                # the whole kit (we don't want a half-mapped kit).
                component_ids = []
                missing = []
                for component_sku in components:
                    cur.execute(
                        "SELECT vendor_sku_id FROM lpg.vendor_skus "
                        "WHERE vendor_id = %s AND vendor_sku_code = %s",
                        (vendor_id, component_sku),
                    )
                    r = cur.fetchone()
                    if r is None:
                        missing.append(component_sku)
                    else:
                        component_ids.append((component_sku, r[0]))

                if missing:
                    print(f"  SKIP {storefront_sku}: "
                          f"unknown components {missing}")
                    stats["skipped_missing_component"] += 1
                    continue

                # Insert one product_components row per component.
                # Ensure the storefront product stub exists (FK requirement
                # per ADR-0005). Mirrors the ingest behavior from ADR-0010.
                cur.execute(
                    "INSERT INTO shift4.products (sku) VALUES (%s) "
                    "ON CONFLICT (sku) DO NOTHING",
                    (storefront_sku,),
                )
                try:
                    for sort_order, (component_sku, vendor_sku_id) in \
                            enumerate(component_ids, start=1):
                        cur.execute(
                            """
                            INSERT INTO lpg.product_components (
                                product_sku, vendor_sku_id, quantity,
                                sort_order, notes
                            ) VALUES (%s, %s, %s, %s, %s)
                            """,
                            (
                                storefront_sku, vendor_sku_id, 1,
                                sort_order,
                                f"Kit BOM (auto-seeded from kit-boms-2025-06-01.csv)",
                            ),
                        )
                        stats["inserted"] += 1
                    print(f"  OK   {storefront_sku} = "
                          f"{' + '.join(c for c, _ in component_ids)}")
                except Exception as exc:
                    print(f"  ERROR {storefront_sku}: {exc}")
                    stats["errors"] += 1
                    conn.rollback()
                    cur = conn.cursor()

        cur.close()

    return stats


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python scripts/seed_kit_boms.py <csv-path>")
        return 1

    csv_path = Path(sys.argv[1])
    print(f"Seeding from: {csv_path}")
    stats = seed(csv_path)

    print()
    print("=== Summary ===")
    print(f"  Inserted rows:              {stats['inserted']}")
    print(f"  Skipped (already mapped):   {stats['skipped_existing']}")
    print(f"  Skipped (missing component):{stats['skipped_missing_component']}")
    print(f"  Errors:                     {stats['errors']}")

    return 0 if stats["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())