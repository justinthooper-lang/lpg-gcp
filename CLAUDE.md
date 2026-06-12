# CLAUDE.md — LPG-GCP

Operating guide for Claude Code on this repo. Read this fully at the start of a session.

## What this project is

A GCP back-office for **Lamp Post Globes (LPG)**, a dropship globe business that
sources from **Crown Plastics**. It has two goals at once, and they shape every choice:

1. **Run LPG's real operations** — ingest Shift4 orders, sync Crown invoices, generate and email purchase orders.
2. **Be a transferable consulting reference architecture** — every decision must be corporate-grade and explainable, so it can be lifted into a client engagement. When two approaches work, prefer the one a consultant could defend and hand off.

Storefront is Shift4Shop. The owner, Justin, is a Salesforce Solutions Architect learning GCP — **Salesforce analogies land well** (CPQ/Product Bundles, metadata vs. data, change sets, org-wide defaults).

## Working relationship

- **Make direct best-practice recommendations. Do not present option menus.** Pick the right answer and say why. Push back on bad ideas — that's wanted, not rude.
- **Verify; don't assume.** After any push, confirm against origin. After any infra change, confirm against the live resource. "It should be fine" is not done.
- **Single-line git commit messages.**
- **An ADR for every significant architecture decision** (see ADR discipline below).
- Brief concept explanation before introducing a new tool or pattern.

## The reversible / irreversible gate (most important)

Proceed freely through **reversible** steps; **stop and get explicit approval before irreversible ones.**

- **Proceed without asking:** reading files, `git diff`, `git status`, `terraform plan`, read-only `psql` queries, `gcloud ... describe` / `get-iam-policy`, building locally, running tests/AST checks.
- **Stop and ask first:** `git push`, `terraform apply`, `./scripts/deploy.sh`, `./scripts/deploy-job.sh`, any `gcloud run deploy`, any DB write/migration against a real instance, any Secret Manager write, any Azure/Exchange policy change, deleting anything.

Before any commit: **show the `git diff` and let Justin review it.** Before any `terraform apply`: **show a `plan` that is a clean no-op / expected-only change.** These two gates are non-negotiable.

## Stack & key facts

- **Repo:** `github.com/justinthooper-lang/lpg-gcp` (public), local `~/projects/lpg-gcp`. Always verify pushes against `origin/main`.
- **GCP:** project `lpg-dev-496820`, region `us-west1`.
- **DB:** Cloud SQL Postgres 16, instance `lpg-dev`, database `lpg`. Access locally via Cloud SQL Auth Proxy: `psql -h 127.0.0.1 -U postgres -d lpg` (proxy target `lpg-dev-496820:us-west1:lpg-dev`).
- **App:** FastAPI + pg8000. Shared DB logic is the installable `lpg_common` package — import from there, don't re-implement connections.
- **Cloud Run services** (share one image): `webhook-handler` (public; Shift4 URL token authenticates, not IAM — ADR-0013) and `lpg-admin` (IAM-private; invoker `user:justin.t.hooper@gmail.com`). Currently `v0.17.0`.
- **Cloud Run job:** `crown-invoice-sync`, currently `v0.13.0`.
- **Compute SA:** `388123220900-compute@developer.gserviceaccount.com`.
- **Secrets (Secret Manager):** `shift4-webhook-token`, `azure-graph-client-secret` (read app), `azure-graph-send-secret` (send app). Values never live in code or Terraform state.
- **Azure/M365 (tenant `fa215d01-a503-4496-ae9f-3ab71e89037e`):** read app `c36883bf-a1b7-4e63-8fc1-c965b32d76ce` (Mail.Read), send app `3e9eda8a-84ad-4bfe-bb94-9e3da4a1160d` (Mail.Send). Both `RestrictAccess`-scoped to `customerservice@lamppostglobes.com` via the mail-enabled group `crown-sync-scope@lamppostglobes.com`.
- **Local:** Mac Apple Silicon, zsh. PowerShell 7 (`pwsh`) for Exchange Online admin.

## How to operate it

- **Deploy services:** `./scripts/deploy.sh vX.Y.Z` — builds one image, dual-deploys both services with full declared shape, runs a smoke-test matrix. The script is the single source of truth for service shape (ADR-0020).
- **Deploy job:** `./scripts/deploy-job.sh vX.Y.Z` — declares full job shape and executes once as a live smoke test. Same source-of-truth principle.
- **Admin UI / endpoints:** `gcloud run services proxy lpg-admin --port=8080`, then `localhost:8080`. Or curl with `-H "Authorization: Bearer $(gcloud auth print-identity-token)"`.
- **Versioning:** image tags are immutable (`vX.Y.Z`); bump, don't reuse.

## Architecture invariants (do not violate)

- **`product_components` is an exception list.** A SKU has rows there *only* if it decomposes into *different* component SKUs. No row = passthrough (the SKU is its own component). Don't add self-referential rows.
- **PO generation:** one order line → one PO line. A combo emits one line = joined component SKUs + summed cost + `vendor_sku_id = NULL`; a passthrough self-prices. **Every line description is Shift4's `order_items.description` verbatim.** Fees are entered manually.
- **Idempotency:** Crown invoices dedup on `(vendor_id, vendor_invoice_number)`; POs are idempotent on `po_number`. Preserve these constraints.
- **Schema discipline:** `schema.sql` is the idempotent single source of truth. For non-idempotent DDL use the `DO $$ BEGIN ... EXCEPTION WHEN duplicate_object` guard pattern.

## Crown Plastics quirks (real, load-bearing)

- **Invoice PDF labels are counterintuitive:** "Sale Amount" = line-item subtotal; "SubTotal" = grand total. Documented in the parser.
- **Two identical invoice emails per invoice**, by design — dedup handles it.
- **Invoice subject marker:** `Invoice/Tracking Information`. Order confirmations say `Order confirmation` — different, so they're filtered out.
- **Mail routing:** invoices land in the `Crown Invoices` Inbox subfolder (an Exchange rule moves them); crown-sync reads only that folder via `CROWN_INVOICE_FOLDER` (set in `deploy-job.sh`). This keeps the fetch window invoice-only.
- **Negotiated fees:** `min_order_fee=$15`, `broken_carton_fee=$15`, `min_order_threshold=$100` (published rate is $25 — use the negotiated values).

## Terraform

- **Boundary (ADR-0019, ADR-0020):** Terraform owns durable infrastructure (DB, Artifact Registry, buckets, IAM, secret *resources*). Cloud Run services + job are **application delivery** and stay script-managed via the deploy scripts. Secret *values* and the Azure apps are never in Terraform.
- **State:** GCS bucket `lpg-dev-496820-tfstate` (created out-of-band — bootstrap exception).
- **Currently manages:** backend, providers, versions, variables, `storage` (PO bucket + IAM), `outputs`, `artifact_registry`, `cloud_sql`.
- **Import workflow (plan-gated, every time):** `describe` the live resource → write lean HCL (required + non-default fields only; most fields are Optional+Computed and adopt the API value) + an `import {}` block → `terraform plan` until it shows **1 to import, 0 to change, 0 to destroy** → `apply` → **remove the import block** (it's one-time scaffolding; leaving it breaks a fresh clone) → `plan` shows no changes → commit. **Any proposed change or replacement = stop and fix the HCL.** Cloud SQL specifically: `database_flags` must be declared or Terraform proposes removing the IAM-auth flag.

## ADR discipline

- ADRs live in `docs/decisions/NNNN-title.md`, append-only. To change a past decision, write a new ADR that supersedes it; don't rewrite history. Verification results can be appended as a dated section.
- Keep the index table in `docs/decisions/README.md` current (it's been missed before — add the row when you add an ADR).
- House format: Status, Date, Context, Decision, Consequences (positive/negative), Future work, References. Present alternatives fairly even when recommending against them.

## Gotchas (hard-won)

- **macOS `sed -i` needs an explicit arg:** `sed -i ''`.
- **Smart-quote auto-conversion breaks SQL.** Use heredocs: `psql ... <<'SQL'`.
- **Storing a secret:** `pbpaste | tr -d '\n\r' | gcloud secrets create NAME --data-file=-`. Verify the stored Value is ~40 chars, not the ~102-char Secret ID.
- **`gcloud run ... deploy --set-env-vars` is the full declaration** — the deploy script is the source of truth; out-of-band `update` env edits get wiped on next deploy. Put env in the script.
- **FastAPI default 404 is `{"detail":"Not Found"}`** vs. our app's `{"error":...}`. If you see the former, the route isn't in the deployed image.
- **`ApplicationAccessPolicy` propagation can take hours**, and `Test-ApplicationAccessPolicy` reports `Granted`/`Denied` from directory state *instantly* regardless of enforcement state — so a `Test` result is corroboration, not proof of live enforcement.
- After editing a Python file, run a quick syntax/AST check before relying on it.

## Where to look first

- `docs/decisions/` — the why behind everything (ADR-0001 … 0020).
- `BACKLOG.md` — what's next, watch items, and open threads.
- `docs/architecture.md` — system overview.
- `scripts/deploy.sh`, `scripts/deploy-job.sh` — the deploy source of truth.
- `terraform/` — the IaC-managed infra.
