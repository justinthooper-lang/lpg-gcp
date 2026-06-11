# LPG-GCP — Backlog / Plan

Working backlog for the GCP back-office. Decisions live in `docs/decisions/` (ADRs);
this file tracks *what's next*, not *what was decided*.

---

## Recently completed (2026-06-11)

PO-generation pipeline (ADR-0018) built, deployed, and prod-verified end-to-end on `v0.14.0`:
generate → persist → render PDF → **send via Graph Mail.Send** (real PO emailed and received).
Separate send-only Azure app with mailbox-scoped Application Access Policy; manual-only send with
a `409` double-send guard. ADR-0018 accepted (Q1 + Q3 resolved). Crown `po_email` reset to NULL
(safe — a stray send now `422`s rather than emailing anyone).

---

## Active / next

### Admin-UI composer modal (the manual send workflow)
The send endpoint is live but currently driven by a raw `curl`. Build the popup that replaces it:
on the order detail page, **Generate PO → preview the PDF → Send** (the button calls the existing
`POST .../send`). Send stays manual (never auto-send); email template is fixed (no per-send editing).
- [ ] "Generate PO" action on the order detail HTML page → `POST /orders/{id}/purchase-order`
- [ ] Inline PDF preview from `GET /purchase-orders/{po_number}/pdf`
- [ ] "Send" button → `POST /purchase-orders/{po_number}/send`, surface 409/422/502 cleanly
- [ ] Set the recipient deliberately (vendor `po_email`) — guard against sending to a test address

### Mailbox hygiene — forwarding clutter + invoice subfolder
**Problem.** The personal-account auto-forward rule forwards *both* Crown invoices and
order-confirmation emails into `customerservice@lamppostglobes.com`. Beyond clutter, it's a
**correctness risk for crown-sync**: its query (`/users/{mailbox}/messages?$top=50`, newest-first,
filtered client-side) reads only the 50 most-recent messages across the whole mailbox, so clutter
competes for slots — enough non-invoice mail between runs could push a real invoice out of the
window and it would be **silently missed**.

**Part A — order confirmations: stop forwarding entirely.** Tighten the personal-account
auto-forward rule so it forwards **only Crown invoices** (subject signature + attachment).
Confirmations should never reach `customerservice@`.

**Part B — invoices to a subfolder, sync reads it directly.** Subfolder: **`Crown Invoices`**.
- [ ] M365 inbox rule on `customerservice@`: move Crown invoices → `Crown Invoices` subfolder
- [ ] Update `fetch_crown_messages` to read `/users/{mailbox}/mailFolders/{folderId}/messages` so the `$top=50` window holds only invoices (removes the clutter-contention risk)
- [ ] Resolve folder by id / well-known-name path; handle folder-not-found
- [ ] Confirm `ApplicationAccessPolicy` (Mail.Read) still satisfied — subfolder is same mailbox scope

**Notes.** Moving invoices to a subfolder does **not** break the current `/messages` query (it spans
all folders); the folder-targeted read is the *improvement*. A and B are complementary: A removes the
cause at the source, B makes the sync structurally immune regardless of other mail. Touches ADR-0016 /
ADR-0017 mailbox topology → warrants an ADR addendum once it lands. Work split: Exchange config = manual
in M365; query change = code, built together.

---

## Backlog

- [ ] **GCS storage** for generated PO PDFs (ADR-0018 Q5, still open). Currently rendered on demand from stored rows; add a bucket + order/PO linkage if/when durable copies are wanted.
- [ ] **Terraform** the new PO infra (PO tables, send app/permissions, GCS bucket) — first real *use* of the foundation `233e289` (manages zero resources today). PO-gen resources written in TF from birth per the "defer importing existing infra" strategy.
- [ ] **ADR-0019** — record the Terraform foundation + its import-deferral strategy (foundation committed but undocumented).
- [ ] **ADR-0017 deferred** — verify the read app's `ApplicationAccessPolicy` scope lockdown is fully propagated / in effect in production.

## Watch items (data quality, not bugs)

- Ingest a real **combo** order so PO explosion is exercised in prod (dev DB has only passthrough orders).
- Ingested orders are missing `ship_to_*` (PDF degrades to "(no ship-to on order)").
- PO PDF `Date` = render date, not a fixed issue date — revisit if Crown needs a stable date (store a header issue date).
- New-combo passthrough gap (ADR-0010 / ADR-0018 watch note): a combo SKU not yet in `product_components` stubs in and silently passes through to Crown. Consider a guard/report flagging combo-shaped SKUs with no BOM rows.
