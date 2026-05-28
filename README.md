# lpg-gcp

Rebuild of **Lamp Post Globes** (LPG) ecommerce CRM on Google Cloud Platform.

## What this is

LPG is a small B2C+B2B ecommerce business selling lamp post globes. The
storefront runs on **Shift4Shop**. Today, the operational CRM/back-office is
on Salesforce. This repo is a from-scratch rebuild on GCP, primarily as a
learning project — the maintainer is a Salesforce architect picking up GCP
by doing.

The goal is not to migrate users off Salesforce overnight. The goal is to
build the same capabilities (and a few that Salesforce doesn't do well) on
GCP, one piece at a time, and learn cloud infrastructure properly along the
way.

## Current state

- **GCP project:** `lpg-dev-496820` (dev environment; no prod yet)
- **Database design:** Cloud SQL Postgres 16, schema defined in
  [`schema.sql`](./schema.sql). Not yet provisioned — schema file is the
  source of truth until we stand up the actual Cloud SQL instance.
- **Storefront integration:** Shift4Shop webhooks → `shift4.*` mirror
  tables. Webhook handler not built yet.
- **Back-office:** vendors, vendor SKUs, and bill-of-materials tables
  defined under `lpg.*`. Purchase orders, invoices, and RGAs still to come.

## Stack

| Layer | Choice | Why |
|---|---|---|
| Cloud | GCP | Learning goal |
| Database | Cloud SQL Postgres 16 | Managed, standard SQL, room to grow |
| Compute | Cloud Run (planned) | Serverless, no VMs to babysit |
| Messaging | Pub/Sub (planned) | Decouple webhook ingest from processing |
| Secrets | Secret Manager (planned) | Don't put credentials in code |
| IaC | gcloud CLI first, Terraform later | Build muscle memory before automating |
| Local dev | Mac (Apple Silicon), Cursor editor | |

## Repo layout

```
lpg-gcp/
├── README.md              # You are here
├── schema.sql             # Postgres schema, source of truth
├── docs/
│   ├── architecture.md    # System design and source-of-truth rules
│   └── decisions/         # Architecture Decision Records (ADRs)
│       ├── README.md      # Index of ADRs
│       └── NNNN-*.md      # One file per decision
└── .gitignore
```

## How to read this repo (humans and AI assistants)

If you're picking up this project — including future LLM sessions — read
these three things in order, before doing anything else:

1. This README — what the project is and where it stands
2. [`docs/architecture.md`](./docs/architecture.md) — how the system is
   designed and the rules that hold it together
3. [`docs/decisions/`](./docs/decisions/) — the **why** behind every
   non-obvious choice

Every working session should end with a doc update if anything changed:
new ADR for new decisions, edits to architecture.md if the design shifted,
this README's "Current state" updated to reflect reality.

## Running the schema locally (not yet wired up)

The schema is designed to apply cleanly via `psql` against a Postgres 16
database. Once Cloud SQL is provisioned, the workflow will be:

```bash
# Connect via Cloud SQL Auth Proxy (TBD)
psql -h 127.0.0.1 -U lpg_admin -d lpg < schema.sql
```

For local development, a Docker Postgres works the same way. Not set up
yet — flagged here as a known next step.

## License

MIT — see [LICENSE](./LICENSE) if/when added.
