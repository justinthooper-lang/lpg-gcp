# LPG-GCP — Backlog / Plan

Working backlog for the GCP back-office. Decisions live in `docs/decisions/` (ADRs);
this file tracks *what's next*, not *what was decided*.

---

## Recently completed (2026-06-11)

A full day. The purchase-order epic (ADR-0018) went from nothing to a deployed,
vendor-emailing system, and the mailbox-hygiene fix landed too. All prod-verified:

- **PO pipeline** — generate → persist → render PDF → **send via Graph Mail.Send**,
  on `lpg-admin` (admin-only endpoints). Manual generate, manual send, `409` double-send guard.
- **Separate send-only Azure app** — `Mail.Send` only, mailbox-scoped Application Access
  Policy, secret in Secret Manager, creds on `lpg-admin`. A real PO emailed and received.
- **Admin-UI composer** — Generate → inline PDF preview → Send, on the order detail page;
  confirm gate + clean 409/422/502 handling. Replaces the curl.
- **GCS archive** — on send, the exact emailed PDF is archived to a Terraform-managed bucket
  (immutable, uniquely-named per send) and the `gs://` URI recorded on the PO. Best-effort:
  a storage failure never unwinds a real send.
- **Terraform foundation activated** — `terraform/storage.tf` is its first managed resource
  (PO PDF bucket + bucket-scoped IAM). `apply` succeeded; state in the GCS backend.
- **Mailbox hygiene** — (A) personal-account forward rule tightened to invoices only
  (subject `Invoice/Tracking Information`), so order confirmations no longer reach
  `customerservice@`; (B) invoices routed to an Inbox subfolder `Crown Invoices`, and
  crown-sync repointed to read only that folder (`CROWN_INVOICE_FOLDER`, set in
  `deploy-job.sh`). Verified live: job read 10 invoices from the folder, 0 non-invoices.
  Removes the silent-invoice-miss risk from the `$top=50` window.
- **ADR-0018 accepted** (Q1 separate send app, Q3 full schema); `BACKLOG.md` committed.

Versions live: services `v0.16.0`, crown-sync job `v0.13.0`. Crown `po_email` reset to NULL
(safe — a stray send `422`s rather than emailing anyone).

---

## Active / next

### Widen Terraform coverage
The bucket is Terraform-managed, but the rest of the new infra was built by hand (gcloud/
portal). Bring it under Terraform so the system is IaC-managed, not just the bucket —
honoring ADR-0018's "new infra in Terraform from birth" intent retroactively where feasible.
- [ ] Codify the GCS `secretAccessor` / bucket IAM already created (some is in `storage.tf`; audit for drift)
- [ ] Decide import vs. document-only for the manually-made pieces: the **send Azure app** (out-of-band, not GCP — likely stays documented, not TF), Secret Manager secret `azure-graph-send-secret`, the `lpg-admin` env/secret wiring, Cloud Run service config
- [ ] `terraform plan` should report **no drift** once the intended resources are codified
- [ ] Note: Cloud Run services are currently deployed via `deploy.sh` (imperative); decide whether they move under TF or stay script-managed (a real reference-architecture fork worth recording)

### ADR-0019 — Terraform foundation + import-deferral strategy
- [ ] Document the foundation (`233e289`), why it managed zero resources, the "write new resources from birth / defer importing existing infra" strategy, and the bucket as its first use.

---

## Backlog

- [ ] **ADR-0017 deferred** — verify the *read* app's `ApplicationAccessPolicy` scope lockdown is fully propagated / in effect in production (the send app's policy is confirmed working via the live send; the read app's was never re-verified post-propagation).

## Watch items (data quality, not bugs)

- Ingest a real **combo** order so PO explosion is exercised in prod (dev DB has only passthrough orders).
- Ingested orders are missing `ship_to_*` (PDF degrades to "(no ship-to on order)").
- PO PDF `Date` = render date, not a fixed issue date — but the **archived** sent PDF now freezes the date at send time, so the audit copy is stable. Revisit only if Crown needs a fixed date on regenerated previews.
- New-combo passthrough gap (ADR-0010 / ADR-0018 watch note): a combo SKU not yet in `product_components` stubs in and silently passes through to Crown. Consider a guard/report flagging combo-shaped SKUs with no BOM rows.
