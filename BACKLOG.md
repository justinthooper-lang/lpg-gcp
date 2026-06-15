# LPG-GCP — Backlog / Plan

Working backlog for the GCP back-office. Decisions live in `docs/decisions/` (ADRs);
this file tracks *what's next*, not *what was decided*.

---

## Recently completed (2026-06-15)

- **ADR-0021 — order field overrides** — LPG-owned corrections overlay the
  read-only `shift4.orders` mirror instead of editing it in place (which
  re-ingest would silently clobber). New `lpg.order_overrides` table + the
  `lpg.v_orders_effective` view (COALESCE override over mirror); the PO builder,
  order list, and order detail now read the view, so a correction flows straight
  into generated POs. Admin order-detail page gains an override editor (writes
  only to `lpg.order_overrides`; blank = inherit, clear-all = revert). Scope is
  addresses/contact/comments only — totals + status stay non-overridable to
  protect Crown-invoice reconciliation. Migration applied to `lpg-dev`
  (idempotent, dry-run-validated); shipped in services `v0.18.0`. Verified: 15
  handler checks + the deploy smoke matrix + live route checks (admin 200, public
  404).

---

## Recently completed (through 2026-06-12)

The Terraform-coverage and Cloud-Run-fork work, on top of the PO epic and mailbox
hygiene from the prior day. All landed and verified:

- **PO epic (ADR-0018)** — generate → persist → render PDF → send via Graph
  Mail.Send on `lpg-admin`, separate send-only Azure app, admin-UI composer, and
  per-send GCS archival of the exact emailed PDF. Prod-verified end to end.
- **Mailbox hygiene** — confirmations no longer forward; invoices route to an
  Inbox subfolder `Crown Invoices`; crown-sync reads only that folder
  (`CROWN_INVOICE_FOLDER` in `deploy-job.sh`). Removes the silent-invoice-miss
  risk from the `$top` window.
- **ADR-0019 — Terraform foundation + import-deferral strategy** — documents the
  foundation, why it managed zero resources, and the import-vs-document fork
  table giving every existing resource a disposition. Index backfilled (0017–0019).
- **Terraform imports (plan-gated, clean)** — Artifact Registry repo `lpg-images`
  and the Cloud SQL instance `lpg-dev` (with `deletion_protection`, IAM auth flag
  preserved, data untouched). Import scaffolding removed; a fresh clone in another
  project would *create* these, not choke on stale import blocks.
- **ADR-0020 — Cloud Run stays script-managed** — Terraform owns durable infra;
  Cloud Run is application delivery, owned by the deploy scripts. Considered the
  TF-with-`ignore_changes` alternative and rejected it for this scale.
- **`deploy.sh` upgraded to full service shape** — both services' identity,
  scaling, Cloud SQL, env, secrets, and IAM invoker policy are now declared on
  every deploy (mirroring `deploy-job.sh`), closing the gap where `deploy.sh` set
  only `--image`. Verified: a `v0.17.0` redeploy reproduced the live env/secrets/
  IAM exactly; all smoke checks passed.
- **ADR-0017 read-app verification** — the read app's `RestrictAccess` policy
  confirmed (policy state + scope-group-of-one + live positive case + a
  `Test-ApplicationAccessPolicy` deny). Live Graph 403 not obtainable for lack of
  a clean out-of-scope mailbox; recorded as a known limitation in ADR-0017.

`terraform/` now manages: backend, providers, versions, variables, storage (PO
bucket + IAM), outputs, artifact_registry, cloud_sql. Services live `v0.18.0`,
crown-sync job `v0.13.0`. Crown `po_email` is NULL (a stray send `422`s).

---

## Active / next

### Finish Terraform coverage of durable infra (optional, low priority)
Per ADR-0019's dispositions, the remaining import candidates are stable and
low-risk. None urgent — the high-value imports (DB, registry) are done.
- [ ] Import the **Secret Manager secret resources** (the secret *values* stay
  out-of-band — never in TF state). Demonstrates the values-out pattern.
- [ ] Import the **project IAM bindings** on the compute SA (intersects the
  per-SA split flagged in ADR-0011).
- [ ] After any of the above, `terraform plan` should report **no drift**.

### Cloud Run fork — DECIDED (ADR-0020), no action
Services + job stay script-managed; revisit only if a CI pipeline/team makes
declarative drift-detection on service config worth a second model.

---

## Backlog

- [ ] **Live Graph 403 for the read app** (optional, to fully close the ADR-0017
  verification): provision a throwaway second mailbox (shared mailbox, no license)
  outside "Crown Invoice Sync Scope", acquire an app token, and confirm a `403
  ApplicationAccessPolicy` against it. Then delete the mailbox.
- [ ] **Document the Azure apps** (read + send) as out-of-band, wrong-provider
  resources (ADR-0019 says document-only, not TF) — a short note if not already
  covered by ADR-0017/0018.
- [ ] Secret rotation playbook; Crown direct-to-tenant delivery (carried forward
  from ADR-0017).

## Watch items (data quality, not bugs)

- Ingest a real **combo** order so PO explosion is exercised in prod (dev DB has only passthrough orders).
- Ingested orders are missing `ship_to_*` (PDF degrades to "(no ship-to on order)").
  Now fixable per-order via the ADR-0021 override editor on the admin order page;
  the upstream gap (Shift4 not sending ship-to at order-created time) still stands.
- PO PDF `Date` = render date, not a fixed issue date — but the **archived** sent PDF freezes the date at send time, so the audit copy is stable. Revisit only if Crown needs a fixed date on regenerated previews.
- New-combo passthrough gap (ADR-0010 / ADR-0018 watch note): a combo SKU not yet in `product_components` stubs in and silently passes through to Crown. Consider a guard/report flagging combo-shaped SKUs with no BOM rows.
- Stray empty `Testing` folder created in `customerservice@` during the ADR-0017 verification — delete at leisure (harmless).
