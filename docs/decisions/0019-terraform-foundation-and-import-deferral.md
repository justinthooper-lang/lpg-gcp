# ADR-0019: Terraform foundation and the import-deferral strategy

**Status:** Accepted
**Date:** 2026-06-12

## Context

The reference-architecture goal wants the infrastructure to be reproducible
as code — a client cloning this repo should be able to stand the system up
in their own project, and a reviewer should be able to read the infra the
same way they read the application.

But the system was *not* built that way. Every piece of GCP infrastructure
so far was created imperatively — `gcloud` commands and console clicks —
across a string of earlier decisions: the Cloud SQL instance (ADR-0008),
the Cloud Run services and their IAM/secret wiring (ADR-0011, ADR-0015),
IAM database auth (ADR-0012), the crown-sync job (ADR-0016/0017), and the
PO-generation send app + bucket (ADR-0018). On top of that sit two Microsoft
Entra app registrations (the read app and the send app) that aren't GCP
resources at all.

A Terraform foundation was committed (`233e289`) — backend, provider,
versions, variables — but it deliberately **managed zero resources**. This
ADR explains that choice and sets the strategy for how Terraform coverage
grows from here, because "we have Terraform now" raises an obvious question:
why does it manage nothing, and what is the plan for the large estate that
already exists outside it?

## Decision

### The foundation (`233e289`)

Four files, no resources:

- **`backend.tf`** — remote state in GCS (`lpg-dev-496820-tfstate`, prefix
  `terraform/state`). State is durable, versioned, lockable, and not tied to
  one laptop. The state bucket itself was created out-of-band, on purpose:
  a backend cannot create the bucket that holds its own state (chicken-and-egg).
  This is the standard Terraform bootstrap exception.
- **`providers.tf`** — the `google` provider, with `project` and `region`
  from variables so the same config can target a different project.
- **`versions.tf`** — pins Terraform (`>= 1.15.0`) and the google provider
  (`~> 6.0`) so a clone gets the toolchain we validated against.
- **`variables.tf`** — `project_id`, `region`, `cloud_run_service_account`,
  all defaulted to the current dev project but overridable (the
  transferability the reference architecture wants).

A foundation that manages nothing is intentional, not unfinished. It
establishes *where* Terraform-managed infra will live and *how* state is
held, without committing to the high-risk act of importing the entire
existing estate in one motion.

### The strategy: new infra in Terraform from birth; defer importing existing infra

Two rules:

1. **New resources are written in Terraform from the start.** Anything
   created from the foundation forward is authored as HCL and applied,
   never clicked. The first instance is the PO PDF bucket and its
   bucket-scoped IAM (`terraform/storage.tf`, ADR-0018 step 8, commit
   `f3d30c0`) — `plan` showed `3 to add, 0 to change, 0 to destroy`, and
   `apply` succeeded with state recorded in the GCS backend.

2. **Existing imperatively-created resources are NOT bulk-imported now.**
   They stay script/`gcloud`-managed until there is a concrete reason to
   bring a specific resource under Terraform.

**Why defer the import rather than do a big-bang migration:**

- `terraform import` is unforgiving — the HCL must match the live resource
  *exactly*, or the next `plan` shows spurious diffs or, worse, a destroy/
  recreate. Reverse-engineering that HCL for a dozen live, in-use resources
  is slow and error-prone.
- The riskiest resources to import are the ones we least want to disturb —
  the Cloud SQL instance holding real data, the live Cloud Run services
  serving traffic. A botched import that proposes replacement is a genuine
  outage.
- Mid-build, the value of importing already-working infra is low. The IaC
  win that matters — *new* infra being reproducible — is captured fully by
  rule 1 without touching anything live.

### The import-vs-document fork

Not everything outside Terraform should eventually move into it. Each piece
of existing infra falls into one of three dispositions:

| Resource | Disposition | Why |
|---|---|---|
| PO PDF bucket + IAM | **In Terraform** (done) | Created new from birth; `terraform/storage.tf`. |
| Cloud SQL instance `lpg-dev` | **Import candidate** (deferred) | Stable, long-lived, doesn't churn — a good early import target once we choose to expand coverage. |
| Artifact Registry repo `lpg-images` | **Import candidate** (deferred) | Stable; rarely changes. |
| Project IAM bindings on the compute SA | **Import candidate** (deferred) | Stable, but entangled with the shared compute SA; intersects the per-SA split flagged as future work in ADR-0011. |
| Secret Manager secrets | **Resources importable; values never in TF** | The secret *resources* could be imported, but the secret *values* stay out-of-band in Secret Manager — Terraform state is not a place for live credentials. |
| Cloud Run services (`webhook-handler`, `lpg-admin`) | **Stay script-managed** (`deploy.sh`) — revisit | Imperative deploy gives fast version-tagged rollouts plus the smoke matrix; modeling per-deploy image-tag churn in Terraform is awkward and fights the workflow. A genuine fork (see Future work). |
| Cloud Run job (`crown-invoice-sync`) | **Stay script-managed** (`deploy-job.sh`) | Same reasoning; its env is already declared in the script as the single source of truth for the job's shape. |
| Azure apps (read + send) | **Document-only — not Terraform** | Microsoft Entra resources, not GCP. The `google` provider can't manage them; full IaC would need the `azuread` provider, which is out of scope here. They live in ADR-0017 / ADR-0018. |
| tfstate bucket `lpg-dev-496820-tfstate` | **Stays out-of-band** (bootstrap) | The backend can't create its own state bucket. Standard exception. |

The point of the table is that "everything should be in Terraform eventually"
is *not* the decision. Some resources are import candidates, some are
deliberately script-managed because the imperative workflow serves them
better, and some can't be in the google provider at all. This ADR is the map
of which is which.

## Consequences

**Positive:**

- New infrastructure is IaC from day one and transferable — point the
  variables at another project and `apply`.
- No risky big-bang import of live, in-use infra; nothing currently serving
  traffic or holding data is at risk of a Terraform-proposed replacement.
- The disposition of every existing resource is explicit, so future-me knows
  what is Terraform-managed, what is script-managed, and what is out-of-band
  — and why.

**Negative:**

- The estate is split-brain: managed in two places (Terraform for new,
  scripts/`gcloud` for existing) until imports happen. A reviewer must know
  which is which — this ADR is that reference.
- `terraform plan` reflects only the Terraform-managed subset; it is **not**
  a full picture of the deployed estate, and a clean plan does not mean the
  whole system is drift-free.
- Drift on the script-managed side (someone clicks a change in the console)
  is not caught by `terraform plan`. The deploy scripts being the source of
  truth for their resources only holds if changes go through them.

## Future work

- **Import Cloud SQL and Artifact Registry** as the next coverage expansion —
  stable, low-churn, low-risk import targets.
- **Decide the Cloud Run services/job fork:** stay script-managed, or move
  under Terraform (likely with image tags as variables and deploys still
  driven by `gcloud`/CI). Record the outcome in a follow-up ADR — this is a
  real reference-architecture choice, not an oversight.
- **Per-service-account split** (ADR-0011 future work) intersects with putting
  IAM in Terraform; do them together.
- **`azuread` provider** for the Entra apps only if full cross-cloud IaC
  coverage becomes a hard requirement; otherwise they stay documented.

## References

- Foundation: [`terraform/backend.tf`](../../terraform/backend.tf),
  [`providers.tf`](../../terraform/providers.tf),
  [`versions.tf`](../../terraform/versions.tf),
  [`variables.tf`](../../terraform/variables.tf) — commit `233e289`
- First managed resources: [`terraform/storage.tf`](../../terraform/storage.tf),
  [`outputs.tf`](../../terraform/outputs.tf) — commit `f3d30c0`
- State backend bucket: `lpg-dev-496820-tfstate` (out-of-band)
- Related ADRs:
  [ADR-0008](./0008-cloud-sql-provisioning.md) (Cloud SQL),
  [ADR-0011](./0011-cloud-run-deploy-architecture.md) (Cloud Run deploy),
  [ADR-0015](./0015-split-webhook-and-admin-services.md) (service split),
  [ADR-0018](./0018-purchase-order-generation.md) (PO generation; step 8 created the bucket)
