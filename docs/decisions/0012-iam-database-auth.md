# ADR-0012: IAM database authentication on Cloud Run

**Status:** Accepted
**Date:** 2026-06-02
**Foreshadowed by:** [ADR-0011](./0011-cloud-run-deploy-architecture.md)

## Context

In [Layer 4c](./0011-cloud-run-deploy-architecture.md) we deployed
the webhook handler to Cloud Run using password authentication for
Postgres: the `postgres` user password was stored in Secret Manager
and mounted into the Cloud Run service as the `PGPASSWORD`
environment variable. The Cloud SQL Python Connector used this
password to open connections.

This works, but stacks two credential systems for the same trust
relationship:

1. GCP IAM identifies Cloud Run-as-service-account to Google
2. A separate Postgres user/password authenticates Cloud
   Run-the-service to Cloud SQL-the-database

Postgres has no way to know that those two credentials belong to the
same caller. Compromise of either is independently bad: a leaked
`postgres` password gives anyone with network access to the proxy
full DB admin rights, regardless of GCP IAM state.

Cloud SQL supports IAM database authentication: Postgres trusts
short-lived OAuth tokens minted by Google for the service account,
verified against IAM in real time. This collapses the two credential
systems into one.

## Decision

The Cloud Run deployment uses **IAM database authentication**. The
local dev environment continues using password authentication
(developer machines don't have access to service account tokens).

Mode selection is automatic via the `K_SERVICE` environment variable,
which Cloud Run sets but local environments do not.

### Implementation

**Cloud SQL side:**
- `cloudsql.iam_authentication=on` flag enabled on the instance
- Postgres user created for the compute service account:
  `388123220900-compute@developer` (Cloud SQL strips the
  `.gserviceaccount.com` suffix; the database username matches that
  truncated form)
- Schema grants applied to the IAM user:
  - `USAGE` on schemas `shift4` and `lpg`
  - `SELECT, INSERT, UPDATE, DELETE` on all tables in both schemas
  - `USAGE, SELECT` on all sequences (required for `BIGSERIAL` PKs)
  - `ALTER DEFAULT PRIVILEGES` mirrors all of the above for
    future tables/sequences

**IAM side:**
- `roles/cloudsql.instanceUser` granted to the compute service
  account at the project level. This is distinct from
  `roles/cloudsql.client` (admin API access); both are required for
  IAM database auth.

**Application code (`db.py`):**
- Branch on `os.getenv("K_SERVICE")`:
  - **Set (Cloud Run):** Connector call passes
    `user=IAM_USER, enable_iam_auth=True`, no password
  - **Unset (local):** Connector call passes
    `user=postgres, password=PGPASSWORD`

The Connector library handles token fetching and refresh
automatically; the app never sees or manages OAuth tokens.

### Operational note: per-schema grants matter

A first-pass deploy that worked with password auth (postgres = near-
superuser on Cloud SQL) will fail with IAM auth if the IAM user
doesn't have explicit grants on the application schemas. The error
appears as `permission denied for table <name>` from the application
side. The `GRANT ... ON ALL TABLES IN SCHEMA ...` plus
`ALTER DEFAULT PRIVILEGES ...` pair fixes this and future-proofs
new tables.

## Alternatives considered

**Service account password file mounted as env var (status quo).**
Rejected: leaves a long-lived credential in the environment;
rotating requires Secret Manager + redeploy churn.

**Use a per-app non-superuser Postgres user with password.** Closer
to least-privilege but still uses a shared static credential.
Rotation is still required. Strictly worse than IAM auth.

**Don't bother — dev project, low risk.** Rejected because the
goal of this project is to learn production-correct GCP patterns.
"Skip the right thing because it's a dev project" defeats the point.

## Consequences

**Positive:**

- No long-lived Postgres password anywhere in production. Token
  refresh happens automatically inside the Connector library every
  ~60 minutes.
- Single source of truth for "who is the Cloud Run service": GCP IAM.
  Revoking the service account's `cloudsql.instanceUser` role
  immediately blocks DB access; no separate password to also revoke.
- Closer to production-correct posture for future projects. The
  per-schema grants and IAM user pattern reproduces cleanly when we
  spin up `lpg-prod` later.
- The `PGPASSWORD` secret remains for local dev only. Production
  secret rotation pressure is lower.

**Negative:**

- Cloud Run cold-start latency includes an extra OAuth token
  exchange (~50-100ms). Not material for a webhook handler.
- Local dev and production diverge slightly: local connects as
  `postgres` (superuser), production as a restricted IAM user.
  Bugs that depend on permission boundaries won't appear locally.
  Acceptable trade-off; we'll catch them via smoke tests in
  production.
- One more IAM role to grant on every new project setup
  (`cloudsql.instanceUser`). Worth documenting in a project-bootstrap
  runbook eventually.

## Future work

- **ADR-0013** (still open): separate build and runtime service
  accounts. Currently both run as the default Compute SA. A cleaner
  setup gives Cloud Run its own SA with only the runtime roles, and
  Cloud Build its own SA with only the build roles.
- Production project will need a separate IAM user for any local
  developer that needs read-only DB access. That user gets
  `roles/cloudsql.instanceUser` plus a SELECT-only Postgres grant.

## References

- [Cloud SQL IAM auth docs](https://cloud.google.com/sql/docs/postgres/iam-authentication)
- [Connector library `enable_iam_auth` option](https://github.com/GoogleCloudPlatform/cloud-sql-python-connector#automatic-iam-database-authentication)
- Implementation: [`webhook-handler/db.py`](../../webhook-handler/db.py)
- Related ADRs:
  [ADR-0011](./0011-cloud-run-deploy-architecture.md) (deploy arch),
  [ADR-0008](./0008-cloud-sql-provisioning.md) (Cloud SQL config)
