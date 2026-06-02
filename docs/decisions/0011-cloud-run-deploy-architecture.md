# ADR-0011: Webhook handler deploy architecture on Cloud Run

**Status:** Accepted
**Date:** 2026-06-02

## Context

The Shift4 webhook handler needs a deploy target that accepts public
HTTPS traffic, scales to zero when idle, and connects to Cloud SQL
securely. The handler also needs runtime configuration (DB credentials,
HMAC signing secret) that doesn't live in source control.

This ADR captures the production deployment architecture decided and
implemented across Layers 4a-4c. It documents the choices made and,
just as importantly, the alternatives rejected and the operational
lessons learned the hard way.

## Decision

### Compute: Cloud Run (managed)

The handler runs as a containerized HTTP service on Cloud Run.

**Why Cloud Run over alternatives:**
- **vs Cloud Functions:** Cloud Run accepts the same FastAPI app
  unchanged. Cloud Functions would require either using its FastAPI
  framework integration (more lock-in) or rewriting to match Cloud
  Functions' handler signature. Container parity also makes local
  development behave identically to production.
- **vs GKE / Compute Engine:** Cloud Run is fully managed (no node
  pools, no autoscaler configuration, no patching). At our scale —
  a single low-traffic webhook ingestion service — GKE is massive
  overkill.
- **vs Cloud Run on GKE:** Same management overhead as plain GKE
  without the simplicity wins.

**Cloud Run configuration:**
- Region: `us-west1` (matches Cloud SQL region; lower latency)
- Memory: 512 MiB (Python + dependencies need more than the 256 MiB
  default to start cleanly)
- CPU: 1 (explicit; defaults vary by region)
- Authentication: `--allow-unauthenticated` (the HMAC signature on
  webhook bodies is what authenticates real requests; Google IAM
  auth would prevent Shift4 from calling us at all)

### Container build: Cloud Build → Artifact Registry

Images are built remotely by Cloud Build (no local Docker required)
and stored in Artifact Registry.

**Build command pattern:**
```
gcloud builds submit --tag us-west1-docker.pkg.dev/lpg-dev-496820/lpg-images/webhook-handler:vX.Y.Z
```

**Why this over alternatives:**
- **vs local `docker build`:** No Docker Desktop install required;
  builds happen on Google's infrastructure, identical to how CI/CD
  would work. Trades off ~30-60s of upload + build time for zero
  local tooling burden.
- **vs Container Registry (gcr.io):** Artifact Registry is the
  current GCP service; Container Registry is deprecated.

**Image tagging:** Semantic-version tags (`v0.4.4`, `v0.5.0`).
Each meaningful change increments the version. Untagged or
`:latest`-only images are rejected in production deploys — explicit
versions make rollbacks possible.

**`.gcloudignore` required:** Without it, Cloud Build's tarball
upload includes `.venv/` and other local-only directories, ballooning
to ~70MB and risking failure. The file should mirror `.gitignore`
plus build-context-specific exclusions.

### Database connection: Cloud SQL Python Connector

The handler connects to Cloud SQL via the
[`cloud-sql-python-connector`](https://github.com/GoogleCloudPlatform/cloud-sql-python-connector)
library, not directly via pg8000's TCP path or via the Unix socket
that Cloud Run's `--add-cloudsql-instances` provides.

**Why this:**
- **Unified code path between local dev and Cloud Run.** The
  connector library handles both: locally it goes through the Cloud
  SQL Auth Proxy; in Cloud Run it uses the metadata server + Unix
  socket. Identical Python code in `db.py` works in both
  environments.
- **pg8000 cannot use Unix sockets via its `host=` parameter.** We
  hit this in Layer 4c: passing `host=/cloudsql/...` causes pg8000
  to attempt DNS resolution of the path and fail with `gaierror`.
  pg8000 *does* support Unix sockets via a separate `unix_sock=`
  parameter, but the conditional logic to detect Cloud Run vs local
  is awkward — the Connector handles it transparently.
- **Future-proofs IAM database auth.** Switching from password to
  IAM auth is a single-argument change in the Connector call (set
  `enable_iam_auth=True`, drop the password). pg8000 has no such
  primitive.

**Trade-off accepted:** the Connector library adds a runtime
dependency on `google-cloud-sql-python-connector` and its transitive
deps. Worth it for the architecture wins above.

### Secrets: Secret Manager mounted as env vars

Runtime secrets (postgres password, webhook signing secret) live in
Secret Manager. Cloud Run mounts them as environment variables at
container startup via `--set-secrets`.

**Pattern:**
```
--set-secrets="PGPASSWORD=cloudsql-postgres-password:latest,SHIFT4_WEBHOOK_SECRET=shift4-webhook-secret:latest"
```

**Why this over alternatives:**
- **vs hardcoded in Dockerfile or env-vars:** Source control should
  never contain secrets. Env vars set via `--set-env-vars` show up
  in Cloud Run console plaintext.
- **vs runtime calls to Secret Manager from Python:** Adds latency
  to cold starts and complicates testing. Env-var mounts work
  identically to "just an env var" from the app's perspective.

**Pitfall encountered:** secrets stored with trailing newlines
(common when piping via shell command substitution) break things
silently — Postgres rejects passwords with control characters via
SCRAM auth, and HMAC computations differ between local and server.
Always store via `printf '%s' "$value" | gcloud secrets versions
add ...` (no `echo`) or via `--data-file=-` with Ctrl+D termination.

### IAM: project-level role bindings on the compute service account

Cloud Run, Cloud Build, and Artifact Registry all run as the
project's default Compute Engine service account
(`<project_number>-compute@developer.gserviceaccount.com`). That
account needs these roles to function end-to-end:

| Role | Purpose |
|---|---|
| `roles/artifactregistry.writer` | Cloud Build pushes images |
| `roles/storage.objectViewer` | Cloud Build reads source tarballs |
| `roles/logging.logWriter` | Cloud Build streams build logs |
| `roles/cloudsql.client` | Connector library calls sqladmin API |
| `roles/secretmanager.secretAccessor` (per-secret) | Mount secrets at runtime |

**Pitfall encountered:** GCP's IAM defaults tightened in 2024-2025;
the default Compute service account now starts with zero project-level
permissions. Every deploy on a fresh project requires granting these
explicitly. The error messages (`storage.objects.get denied`,
`artifactregistry.repositories.uploadArtifacts denied`,
`cloudsql.instances.get`) are clear once you know to look for them,
but the first encounter is bewildering.

**Trade-off accepted:** the Compute service account is shared across
build/runtime concerns. A cleaner production setup would have
separate service accounts for build vs runtime, each with the minimum
roles. Worth doing in a future ADR once the deploy surface stabilizes.

### Endpoint paths: avoid `/healthz`

Cloud Run's frontend layer intercepts requests to `/healthz` for its
own health-check infrastructure. Application code that exposes
`/healthz` will appear to work locally but return a Google-branded
404 in Cloud Run.

Solution: don't expose `/healthz`. Cloud Run doesn't require an
application health endpoint; it uses its own TCP probe on the
container port. The handler's `/healthz` route from earlier sessions
is technically dead code; left in for now, can be removed.

## Consequences

**Positive:**

- A change-from-laptop-to-production round trip is two commands
  (build + deploy) totaling ~60-90 seconds. Iteration is fast enough
  that production debugging via redeploy is viable.
- Costs are negligible at our scale: Cloud Run scales to zero
  ($0 idle), Cloud Build is metered by build-minute (~$0.003 per
  build), Secret Manager and Artifact Registry are free at our
  volumes.
- Identical Python code runs locally and in production; no
  environment-specific branches in application code.
- All secrets and credentials are off-disk, off-source-control, and
  rotatable without code changes.

**Negative:**

- IAM surface is wide and non-obvious; the first deploy requires
  granting 5+ roles to the Compute service account, and the failure
  modes are misleading until you've seen them.
- Secret Manager's no-trailing-newline requirement is undocumented
  and bites everyone exactly once.
- Debugging a failed deploy requires switching between
  `gcloud run services logs read` (which silently drops JSON
  payloads) and `gcloud logging read` (which preserves them).

## Future work

- **ADR-0012:** IAM database auth via the Connector library
  (`enable_iam_auth=True`); removes the postgres password from
  Secret Manager entirely
- **ADR-0013:** Separate build and runtime service accounts with
  minimum-required role bindings each
- **ADR-0014:** Wire real Shift4 webhooks (resolve actual signature
  header name and encoding; possibly remove the dev-mode no-secret
  bypass entirely)

## References

- Cloud Run service: `webhook-handler` in `us-west1`
- Service URL: `https://webhook-handler-388123220900.us-west1.run.app`
- Image repository: `us-west1-docker.pkg.dev/lpg-dev-496820/lpg-images/webhook-handler`
- Implementation across:
  [`webhook-handler/Dockerfile`](../../webhook-handler/Dockerfile),
  [`webhook-handler/.gcloudignore`](../../webhook-handler/.gcloudignore),
  [`webhook-handler/db.py`](../../webhook-handler/db.py),
  [`webhook-handler/auth.py`](../../webhook-handler/auth.py)
- Related ADRs:
  [ADR-0008](./0008-cloud-sql-provisioning.md) (Cloud SQL config),
  [ADR-0010](./0010-product-stub-auto-create.md) (product stubs)
