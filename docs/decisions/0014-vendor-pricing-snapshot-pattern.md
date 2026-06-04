# ADR-0014: Vendor pricing snapshots — PDF, CSV, seed script

**Status:** Accepted
**Date:** 2026-06-04

## Context

Vendors send price lists as PDFs. The PDF is the legal artifact —
the document we'd point at if a dispute came up over what we were
quoted. But a PDF is useless as data: we can't query it, can't join
it against orders, and can't compute margins from it.

To make pricing actionable we need to translate the PDF into rows
in `lpg.vendor_skus`. There are two implicit decisions here that
are worth being explicit about:

1. **What's the source of truth?** Once data lives in the database,
   the temptation is to treat the DB as the source. But the DB is
   editable by anyone with credentials and isn't versioned in a
   way that's reviewable months later. The PDF *as received from
   the vendor* is the only artifact we can defend.

2. **How do edits propagate when pricing changes?** Crown updates
   their price list periodically. Each update needs to flow into
   `lpg.vendor_skus` without losing history of what we were paying
   before. Overwriting the file in place would destroy the audit
   trail.

This ADR captures the pattern we settled on.

## Decision

**Three layers, never collapse them:**

1. **PDF snapshots** live in `data/<vendor>-price-list-<YYYY-MM-DD>.pdf`.
   The date is the *effective date Crown printed on the document*,
   not the date we received or imported it. Snapshots are never
   overwritten — each new price list lands as a new file. The PDF
   stays in source control so the legal artifact is versioned with
   the system that consumes it.
2. **CSV snapshots** live in `data/<vendor>-skus-<YYYY-MM-DD>.csv`.
   Same date convention. Derived from the PDF by hand the first time;
   future updates may be auto-extracted. The CSV is editable, diff-able,
   and is the actual input to the seed script.
3. **Seed script** lives in `scripts/seed_<vendor>_pricing.py`. Takes
   a CSV path as its only argument. Idempotent via
   `INSERT ... ON CONFLICT DO UPDATE`. Reports inserted/updated/
   skipped/errors counts.

**Workflow when Crown updates pricing:**

1. Save the new PDF as `data/crown-price-list-<YYYY-MM-DD>.pdf` (new
   file, never overwrite the old one)
2. Translate to `data/crown-skus-<YYYY-MM-DD>.csv` matching the new
   PDF
3. `python scripts/seed_crown_pricing.py data/crown-skus-<NEW>.csv`
4. Git diff the CSVs (old vs new) before committing — it surfaces
   exactly which SKUs changed price
5. Commit both new files in the same commit as the seed run

Old PDFs and CSVs stay in `data/` indefinitely. Disk is cheap and
the historical record of what we were paying when matters more than
git repo size.

## CSV schema

```
category, vendor_sku_code, unit_cost, std_pack_qty, std_skid_qty, status, notes
```

- **category** — section header from the PDF (Street Lamps, Spheres,
  etc.). Stored as `lpg.vendor_skus.description` for now. Future
  schema may add a real `category` column; the CSV is forward-compatible.
- **unit_cost** — empty for `call_for_quote` and `discontinued`.
  Never fabricate a value to fill a gap; missing data should look
  missing.
- **std_pack_qty / std_skid_qty** — empty cells mean "not published"
  (often appears as MOQ or TBD in the PDF). The seed script defaults
  `std_pack_qty` to 1 because the schema has a `> 0` check.
- **status** — must match the `lpg.vendor_sku_status` enum. Currently
  `active`, `call_for_quote`, `discontinued`. **CSV values must
  match the enum strings exactly.** We learned this the hard way:
  the first draft used `quote-required` and every row failed silently
  until we fixed it.
- **notes** — free text. Captures PDF context that doesn't fit elsewhere
  ("Polycarbonate", "Loose configuration", "STD SKID = MOQ", etc.).

## Negotiated pricing overrides published pricing

The PDF lists Crown's published fees ($25 MIN ORDER, $25 BROKEN
CARTON). The actual contract is $15/$15 for LPG.

**The schema captures negotiated terms, not published terms.** When
we seeded the `CROWN` vendor row, we used the negotiated values, not
the PDF values. The PDF is the reference document for the catalog;
the database holds the truth about what we pay.

This generalizes: any vendor-specific contract terms (volume
discounts, NET-30 vs NET-60, freight allowances) belong in the
`lpg.vendors` row or a future contract table, never inferred from
the PDF at query time.

## Alternatives considered

**Edit the PDF directly when prices change.** Rejected. PDFs are
opaque, hard to diff, and treating them as mutable destroys the
forensic trail.

**Skip the CSV — auto-parse the PDF on seed-script run.** Rejected
for now. PDF parsing is brittle (multi-column tables, "CALL FOR
QUOTE" interleaved with numeric prices, section headers that
overflow). The first time through requires human review anyway;
the CSV is that human review made durable. May revisit once we
have a tool that produces reliable CSV output from this specific
PDF format.

**Store pricing in YAML/JSON instead of CSV.** Rejected. CSV is
the closest data format to what the PDF actually is (a table) and
opens cleanly in any spreadsheet for spot-checks. JSON would tempt
us to nest structures the PDF doesn't have.

**Make `data/` a separate repo or git LFS.** Rejected at our scale.
The PDFs are ~700KB each and CSVs are ~30KB. Even a decade of
updates is well under a typical repo size budget. Reconsider if
we end up archiving large binary assets.

## Consequences

**Positive:**

- A future price update is mechanical: drop two files, run one
  command, commit. No code changes required.
- Git history shows exactly which SKUs changed price between any
  two dates. Margin reports can be reconstructed historically.
- The PDF and CSV stay aligned because both are dated, both live
  in the same directory, and both are committed in the same commit
  as the seed run.

**Negative:**

- First-time PDF-to-CSV extraction is manual labor. About 30 minutes
  for Crown's catalog (~600 SKUs). Faster the second time when the
  patterns are familiar.
- `lpg.vendor_skus` description column holds category names, which
  isn't quite what `description` should mean. Acceptable for now;
  a proper category column is on the schema roadmap.

## Future work

- Add `category` column to `lpg.vendor_skus` and migrate description
  values into it.
- Build a "diff two CSVs" command that highlights price changes
  (% delta, new SKUs, removed SKUs). Useful when reviewing Crown's
  next update.
- Apply the same pattern to a second vendor when LPG adds one,
  validating the pattern generalizes.

## References

- Implementation:
  - [`data/crown-price-list-2025-06-01.pdf`](../../data/crown-price-list-2025-06-01.pdf)
  - [`data/crown-skus-2025-06-01.csv`](../../data/crown-skus-2025-06-01.csv)
  - [`scripts/seed_crown_pricing.py`](../../scripts/seed_crown_pricing.py)
- Related ADRs:
  [ADR-0003](./0003-vendor-cost-in-vendor-skus.md) (vendor cost on vendor_skus),
  [ADR-0004](./0004-bom-via-product-components.md) (BOM table)
