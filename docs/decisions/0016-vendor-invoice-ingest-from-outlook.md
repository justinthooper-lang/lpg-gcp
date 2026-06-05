# ADR-0016: Ingest Crown invoices via Cloud Run job \+ Microsoft Graph service principal

**Status:** Accepted **Date:** 2026-06-05

## Context

The system today knows what customers were charged (Shift4 webhook data) and what Crown's published list price *says* we should pay (seeded `lpg.vendor_skus`). It does not know what Crown *actually* charged us — the real, per-invoice, post-freight numbers. Without that, every margin number in the system is an estimate.

Crown sends invoices as PDF attachments to `lamppostglobes@outlook.com`. Each PDF carries the per-line cost, freight (UPS or truck), and the customer PO number that joins back to `shift4.orders.invoice_number`. We need to ingest those invoices on a schedule and write the parsed data to Cloud SQL.

A prior Salesforce iteration (`reference/crown_invoice_sync.py`) already solved the hard problems: Microsoft Graph OAuth, Crown's specific PDF format with its multi-column totals block, the invoice-vs-confirmation discrimination, replacement-shipment handling. That script runs on a laptop via delegated (user-signed-in) OAuth. For this iteration we want cloud-deployed, unattended ingestion.

Two architectural decisions converged from earlier discussions:

1. **Service principal auth, not delegated.** The prior script's device-code flow requires interactive re-auth every \~90 days. That's fine for a one-person script on a laptop; it doesn't fit a "transferable to clients" reference architecture (a future consulting engagement won't have Justin sitting at a terminal).  
2. **The `lamppostglobes.com` M365 tenant supports this.** Originally we thought we'd be stuck with a personal Microsoft account (client-credentials disallowed). The `lamppostglobes.com` domain is on a real M365 Business tenant with admin rights — meaning we can register a service principal with `Mail.Read` Application permission, the proper unattended-service path.

## Decision

**Three pieces of infrastructure, one of code:**

1. **A new Cloud Run job: `crown-invoice-sync`.** Cloud Run jobs are the right product variant for scheduled batch work — they run a container to completion and exit, rather than serving HTTP. Triggered by Cloud Scheduler once per day at 2 AM Pacific.  
2. **Schema additions: `lpg.vendor_invoices` \+ `lpg.vendor_invoice_lines`.** One row per Crown invoice, one row per L/I (line item) on each invoice. Joined back to `shift4.orders` by PO number (soft join, no FK; direct Crown orders outside Shift4 still persist).  
3. **Azure app registration** in the `lamppostglobes.com` tenant with `Mail.Read` Application permission. Client secret stored in Google Secret Manager, accessed at runtime by the Cloud Run job's service account.  
4. **The job's Python code** ports the existing `reference/crown_invoice_sync.py`. Replaces:  
   - Delegated/device-code OAuth → MSAL client-credentials flow  
   - Salesforce `simple_salesforce` writes → `pg8000` writes via `webhook-handler/db.py`  
   - Outlook category-tagging idempotency → unchanged, still our defense-in-depth alongside DB unique constraints

## Architecture

```
┌─────────────────┐    cron (daily 2am)
│ Cloud Scheduler ├──────────────────────┐
└─────────────────┘                      │
                                         ▼
                          ┌──────────────────────────────┐
                          │ Cloud Run job:               │
                          │ crown-invoice-sync           │
                          │                              │
                          │  ┌────────────────────────┐  │
                          │  │ Reads:                 │  │
                          │  │  - azure-graph-client- │  │
                          │  │    secret              │  │
                          │  └─────────┬──────────────┘  │
                          │            │                 │
                          │            ▼                 │
                          │  ┌────────────────────────┐  │
                          │  │ MSAL client_creds      │  │
                          │  │ → access token         │  │
                          │  └─────────┬──────────────┘  │
                          └────────────┼─────────────────┘
                                       │
                       ┌───────────────┼───────────────┐
                       ▼                               ▼
            ┌──────────────────┐         ┌──────────────────────┐
            │ Microsoft Graph  │         │ Cloud SQL (Postgres) │
            │  /users/cs@...   │         │   lpg.vendor_invoices│
            │  /messages       │         │   lpg.vendor_invoice_│
            │  + attachments   │         │     lines            │
            └──────────────────┘         └──────────────────────┘
```

## Schema

```sql
CREATE TABLE lpg.vendor_invoices (
    vendor_invoice_id       BIGSERIAL PRIMARY KEY,
    vendor_id               BIGINT NOT NULL REFERENCES lpg.vendors,

    -- Identifiers from the PDF
    vendor_invoice_number   TEXT NOT NULL,    -- "227851"
    vendor_order_number     TEXT,             -- Crown's internal "Order No"
    customer_po_number      TEXT,             -- "PO32150" - soft join key

    -- Dates
    invoice_date            DATE,
    ship_date               DATE,

    -- Shipping
    ship_via                TEXT,             -- "FedEx Ground"
    tracking_numbers        TEXT[],           -- {"872614494042", ...}
    freight_type            TEXT CHECK (freight_type IN ('ups', 'truck') OR freight_type IS NULL),
    freight_truck           NUMERIC(12,2),
    freight_ups             NUMERIC(12,2),

    -- Money (verbatim from PDF totals block)
    subtotal                NUMERIC(12,2),    -- goods only
    sale_amount             NUMERIC(12,2),    -- subtotal + freight
    amount_received         NUMERIC(12,2),
    balance_due             NUMERIC(12,2),

    -- Status / classification
    is_replacement          BOOLEAN NOT NULL DEFAULT FALSE,
    raw_pdf_filename        TEXT,

    -- Sync provenance / idempotency
    graph_message_id        TEXT NOT NULL UNIQUE,
    synced_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_vendor_invoice_number
        UNIQUE (vendor_id, vendor_invoice_number)
);

CREATE INDEX idx_vendor_invoices_po
    ON lpg.vendor_invoices (customer_po_number);
CREATE INDEX idx_vendor_invoices_date
    ON lpg.vendor_invoices (invoice_date);

CREATE TABLE lpg.vendor_invoice_lines (
    vendor_invoice_line_id  BIGSERIAL PRIMARY KEY,
    vendor_invoice_id       BIGINT NOT NULL REFERENCES lpg.vendor_invoices
                                  ON DELETE CASCADE,

    line_number             INTEGER NOT NULL,           -- "L/I" column
    vendor_sku_code         TEXT NOT NULL,              -- "88264-CL-8F"
    vendor_sku_id           BIGINT REFERENCES lpg.vendor_skus,
    qty_shipped             INTEGER NOT NULL,
    qty_backorder           INTEGER NOT NULL DEFAULT 0,
    uom                     TEXT,                       -- "EA"
    unit_price              NUMERIC(12,4),              -- Crown uses 4 decimals
    extended_price          NUMERIC(12,2),

    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_invoice_line
        UNIQUE (vendor_invoice_id, line_number)
);

CREATE INDEX idx_invoice_lines_sku
    ON lpg.vendor_invoice_lines (vendor_sku_code);

-- Trigger to bump updated_at on vendor_invoices, matching pattern
-- on other lpg.* tables (lpg.set_updated_at function).
CREATE TRIGGER trg_vendor_invoices_updated_at
BEFORE UPDATE ON lpg.vendor_invoices
FOR EACH ROW EXECUTE FUNCTION lpg.set_updated_at();
```

**Design notes:**

- `graph_message_id UNIQUE` is the **primary idempotency key**. Re-running the sync is safe even if Outlook tag-write failed.  
- `customer_po_number` is nullable, no FK to `shift4.orders`. The relationship is "Crown's customer-PO field happens to equal our storefront's invoice\_number," not "this row points to that row." Soft join at query time keeps direct-Crown invoices (PO outside our 30000-33000 range) ingestible.  
- `unit_price NUMERIC(12,4)` matches Crown's 4-decimal precision. `extended_price` stays at 2 (that's how it's rendered).  
- `vendor_sku_id` on line items is nullable. PDF may reference a Crown SKU we haven't seeded yet (Crown added it after our last catalog refresh). Persist the line, leave the FK null, an UPDATE join during the next `seed_crown_pricing.py` run can populate it.  
- `is_replacement` matches the existing code's REPL-suffix detection. Replacement invoices are stored but the UI presents them separately and does NOT roll them into the original order's margin math (a customer-service replacement isn't a real sale).

## Sync workflow

The job's container runs `python scripts/sync_crown_invoices.py` once on startup, then exits. Behavior:

1. **Authenticate to Microsoft Graph** via MSAL client-credentials. Reads `AZURE_CLIENT_SECRET` from Secret Manager mount; `AZURE_CLIENT_ID` and `AZURE_TENANT_ID` from env vars.  
2. **Fetch messages** from `customerservice@lamppostglobes.com` inbox over the last N days (default 7, configurable via env `LOOKBACK_DAYS`). Uses `$filter=receivedDateTime ge ...` with pagination.  
3. **Filter client-side**: sender \= `crown@plasticglobes.com`, NOT already tagged `Synced-to-LPG-GCP`.  
4. For each candidate message:  
   - Download PDF attachment(s)  
   - Discriminate invoice vs order-confirmation (skip confirmations without tagging — they may turn into invoices later)  
   - Parse PDF: invoice number, PO, dates, freight, line items, totals  
   - In one transaction:  
     - INSERT into `lpg.vendor_invoices` with `ON CONFLICT (graph_message_id) DO NOTHING` (idempotent re-runs)  
     - If insert happened: DELETE+INSERT lines (matches order\_items pattern from ADR-0009)  
   - Commit. Tag the Graph message as `Synced-to-LPG-GCP`.  
5. Log structured JSON throughout (`inserted`, `updated`, `skipped`, `errors` counts). Non-zero exit on errors.

## Configuration

Cloud Run job env vars (non-secret):

| Var | Value | Notes |
| :---- | :---- | :---- |
| `AZURE_TENANT_ID` | `fa215d01-a503-4496-ae9f-3ab71e89037e` | `lamppostglobes.com` |
| `AZURE_CLIENT_ID` | `c36883bf-a1b7-4e63-8fc1-c965b32d76ce` | "Lamp Post Globes — Crown Invoice Sync" app |
| `TARGET_MAILBOX` | `customerservice@lamppostglobes.com` | The mailbox to read |
| `INSTANCE_CONNECTION_NAME` | `lpg-dev-496820:us-west1:lpg-dev` | Same as webhook-handler |
| `DB_NAME` | `lpg` | Same as webhook-handler |
| `DB_USER` | `postgres` | Same; IAM identity is the actual auth |
| `LOOKBACK_DAYS` | `7` | First production run will override to `30` for backfill |

Mounted secret:

| Secret | Mount as env var |
| :---- | :---- |
| `azure-graph-client-secret` | `AZURE_CLIENT_SECRET` |

Cloud Scheduler config:

| Setting | Value |
| :---- | :---- |
| Schedule | `0 2 * * *` (2 AM Pacific daily) |
| Time zone | `America/Los_Angeles` |
| Target | Cloud Run job `crown-invoice-sync` |
| Auth | OIDC token, service account with `roles/run.invoker` on the job |

## Out of scope

- **Backfill of old invoices.** First production run sets `LOOKBACK_DAYS=30`. To go further back, manual override. Deferred until we see how many historical invoices matter.  
- **Profit fields and dashboard** (the original "feedback" items 3 and 4). Depend on invoice data flowing. Separate ADR.  
- **Forwarding from `lamppostglobes@outlook.com`.** Currently Crown sends to that mailbox. We'd need an Outlook forwarding rule to `customerservice@lamppostglobes.com`, OR ask Crown to update our email of record. Decision deferred — both work; let's see which is easier when we test the first sync run.  
- **Application Access Policy** restricting `Mail.Read` to a single mailbox. Currently the app can read any mailbox in the `lamppostglobes.com` tenant. The tenant has effectively one mailbox in scope (`customerservice@`), so the practical blast radius is unchanged. Locking this down via PowerShell is queued for a follow-up session.

## Alternatives considered

**Local script via `launchd`.** Simpler but ties the business to Justin's laptop being on. Doesn't generalize to a client engagement. Rejected.

**Cloud Run service (always-on HTTP, triggered by Pub/Sub).** Overkill. The sync is bursty (one batch per day), not request-driven. Cloud Run jobs are billed only for the seconds the container runs; services have minimum-instance considerations and complicate the trust model.

**Microsoft Graph webhooks (real-time push).** Would replace the daily-poll model with "Crown email arrives → Graph posts to our endpoint → we ingest immediately." Strictly better for latency, but adds: a public HTTPS endpoint to receive webhooks, subscription renewal (max 3 days for inbox subscriptions, must re-subscribe), and the same auth setup we just did anyway. Deferred unless we ever need sub-day freshness.

**Cloud Functions (gen 2\) instead of Cloud Run job.** Equivalent at our scale; Cloud Run jobs are what we already know and what Anthropic recommends for batch work. Stay consistent.

**Forwarding Crown invoices to Gmail / a webhook ingest endpoint.** Introduces a forwarding rule as a failure point. Skipped in favor of reading Outlook directly.

## Consequences

**Positive:**

- Margin reporting becomes *true*, not estimated. The "Real Cost" column in the order detail view can pivot from list-cost-from-PDF to actual-cost-from-invoice.  
- Per-SKU cost drift over time becomes queryable across the `vendor_invoice_lines` table.  
- Freight reality is captured separately from product cost, enabling the "shipping margin" feedback Justin asked for.  
- Cloud-deployed \= doesn't care if Justin's laptop is on, doesn't care if Justin switches hardware. Survives the laptop-takes-a-dump scenario.  
- The architecture is portable: future clients with a real M365 tenant get the same pattern, with their domain swapped in.  
- We learn Cloud Run jobs \+ Cloud Scheduler, which are the GCP-native answer for scheduled work — directly transferable to consulting engagements.

**Negative:**

- Client secret rotation every 180 days. Calendar reminder needed. (See follow-up work.)  
- Two Microsoft objects we depend on: the app registration and the client secret. If the registration is accidentally deleted, the job stops. Documented here; mitigated by IaC eventually (Terraform module for Azure resources is its own future project).  
- Cloud Run jobs are a different deployment surface than Cloud Run services. `deploy.sh` will need an analogous `deploy-job.sh` or we generalize the script. Worth noting; not blocking.  
- The Outlook category-tagging step requires write access to message metadata, which the `Mail.Read` Application permission does NOT grant. We'll need to either add `Mail.ReadWrite` Application permission (broader than we want) or use a different idempotency strategy (rely solely on the DB `graph_message_id UNIQUE` constraint). Likely the latter; document the choice at implementation time.

## Future work

- **Lock down `Mail.Read` to single-mailbox scope** via PowerShell `New-ApplicationAccessPolicy`. \~10 min one-time. Queued in Claude's memory.  
- **Secret rotation playbook.** A short markdown doc explaining how to rotate `azure-graph-client-secret` when it expires (Azure portal → generate new secret → `gcloud secrets versions add`). Add a calendar reminder to rotate \~30 days before expiry.  
- **Terraform** for the new infrastructure (Cloud Run job, Cloud Scheduler, IAM bindings, env vars). Don't go back and Terraformify the existing webhook-handler / lpg-admin services — build new infra in Terraform from now on, retrofit when the appetite hits.  
- **Profit-margin views** and a back-office dashboard (Justin's original feedback items 3 and 4\) — depend on this invoice data flowing.  
- **Auto-promote `vendor_sku_id`** on existing invoice lines when missing SKUs are later seeded into `lpg.vendor_skus`. Small UPDATE join in the seed script.

## References

- Reference implementation: `reference/crown_invoice_sync.py` (to be committed alongside this ADR for ongoing reference)  
- Related ADRs: [ADR-0003](http://./0003-vendor-cost-in-vendor-skus.md) (vendor cost on vendor\_skus), [ADR-0004](http://./0004-bom-via-product-components.md) (BOM via product\_components), [ADR-0009](http://./0009-shift4-webhook-contract.md) (order\_items DELETE-then-INSERT pattern), [ADR-0011](http://./0011-cloud-run-deploy-architecture.md) (Cloud Run deploy architecture), [ADR-0012](http://./0012-iam-database-auth.md) (IAM database auth on Cloud Run), [ADR-0014](http://./0014-vendor-pricing-snapshot-pattern.md) (vendor pricing snapshots — the list-price layer this complements)  
- External: Microsoft Graph [Mail.Read Application permission docs](https://learn.microsoft.com/en-us/graph/permissions-reference#mailread)  
- Azure objects in `lamppostglobes.com` tenant:  
  - App registration: "Lamp Post Globes — Crown Invoice Sync" (client ID `c36883bf-a1b7-4e63-8fc1-c965b32d76ce`)  
  - Tenant ID: `fa215d01-a503-4496-ae9f-3ab71e89037e`

