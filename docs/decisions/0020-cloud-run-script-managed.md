# ADR-0020: Cloud Run stays script-managed; deploy scripts own the full service shape

**Status:** Accepted
**Date:** 2026-06-12

## Context

ADR-0019 drew a Terraform boundary and explicitly deferred one decision: do the
Cloud Run services (`webhook-handler`, `lpg-admin`) and the job
(`crown-invoice-sync`) move under Terraform, or stay managed by the deploy
scripts? It flagged this as "a real reference-architecture choice, not an
oversight" and promised a follow-up ADR. This is that ADR.

Two facts about the current state drive the decision:

1. **The job is already fully script-declared.** `scripts/deploy-job.sh`
   declares the job's *entire* shape on every deploy — image, service account,
   Cloud SQL attachment, secrets, env vars, retries, timeout — and `gcloud run
   jobs deploy` is create-or-update, so the script is a complete, re-runnable
   single source of truth. This model works well.

2. **The services are only *partially* script-declared.** `scripts/deploy.sh`
   sets only `--image` and `--region` on each `gcloud run deploy`. Everything
   else that defines the services — service account, env vars, secret bindings,
   Cloud SQL attachment, ingress, scaling, and the IAM invoker policy (public
   `allUsers` on `webhook-handler`, IAM-restricted on `lpg-admin`) — was set
   imperatively at some earlier point and now exists **only in the live
   resource**, captured in no re-runnable code. That is the actual gap.

The reflexive move is "import everything into Terraform." This ADR argues
against that for Cloud Run specifically, and fixes the real problem instead.

## Decision

**Terraform's boundary is durable infrastructure. Cloud Run is application
delivery and stays in the deploy scripts — but `deploy.sh` is upgraded to
declare the services' full shape, matching the pattern `deploy-job.sh` already
uses for the job.**

### Where the line falls

- **Terraform owns** the slow-changing substrate: Cloud SQL, Artifact Registry,
  GCS buckets and their IAM, Secret Manager secret *resources*, and project/
  resource IAM bindings. These rarely change, never churn per-deploy, and import
  cleanly (ADR-0019).
- **Deploy scripts own** Cloud Run — services and job alike — as the
  *application delivery* layer. The scripts build and push image tags and assert
  the full runtime shape. `gcloud run deploy` / `jobs deploy` is create-or-update,
  so a script run reconstructs the resource from nothing on a fresh project and
  re-asserts it on redeploys.

### Why not import Cloud Run into Terraform

- **Image churn fights Terraform.** Every code change ships a new image tag. If
  Terraform owned the service, either every deploy runs through `terraform
  apply` (slower than the current ~60–90s build-and-deploy loop, and couples
  app releases to infra state), or the image is excluded with `lifecycle {
  ignore_changes = [...] }` — at which point Terraform no longer manages the one
  field that actually changes, and a second tool (the script) still has to push
  images. That hybrid adds a model without removing one.
- **A second management model for one resource class.** The job is already
  script-declared and works. Importing the *services* into Terraform while the
  *job* stays scripted would mean two different mental models for the same kind
  of resource. Extending the proven script pattern to the services is simpler
  and consistent.
- **The reference-architecture narrative is cleaner as a clean split:**
  *Terraform = infrastructure substrate; deploy scripts = application delivery.*
  That is an honest, explainable boundary a client can adopt, not a hedge.

### What changes as a result

`deploy.sh` is upgraded to declare each service's full shape on deploy — service
account, env vars, secret bindings, Cloud SQL attachment, ingress, scaling, and
(re-)assert the IAM invoker policy — the same way `deploy-job.sh` does for the
job. Today's `--image`-only deploy is the gap this closes: after the upgrade,
the services are reproducible from code, not just from the live resource.

### Alternative considered: import into Terraform with `ignore_changes` on image

The mainstream "everything in IaC" approach: `google_cloud_run_v2_service` /
`google_cloud_run_v2_job` resources with `lifecycle { ignore_changes =
[template[0].containers[0].image] }`, Terraform owning the skeleton and the
scripts pushing images. This gives `terraform plan` drift detection on the
service shape. It was rejected for LPG because it keeps *both* tools in the loop
(Terraform for the skeleton, scripts for images), introduces the image-exclusion
caveat, and splits the Cloud Run management model across two systems for no gain
that the full-shape script doesn't already provide at this scale. Worth
revisiting if LPG grows a team or a CI system where plan-time drift detection on
service config becomes valuable.

## Consequences

**Positive:**

- One management model for all of Cloud Run (services and job): the deploy
  scripts, each a complete re-runnable source of truth.
- The fast deploy loop is preserved — no `terraform apply` coupled to app
  releases, no image-churn impedance mismatch.
- The real gap is closed: upgrading `deploy.sh` makes the services' full shape
  reproducible from code, which is the transferability the reference
  architecture actually needs.
- The Terraform boundary is crisp and explainable: durable infra vs. app delivery.

**Negative:**

- `terraform plan` does not cover Cloud Run, so it is not a full-estate view
  (same caveat ADR-0019 already records for the script-managed surface).
- Drift on a service *between* deploys isn't caught declaratively — but each
  deploy re-asserts the full shape, overwriting drift, exactly as the job model
  already does.
- The IAM invoker policy is security-relevant and now lives in `deploy.sh`;
  it must be asserted on every deploy, not assumed. The upgrade must handle it
  explicitly (especially `allUsers` on `webhook-handler` vs. restricted
  `lpg-admin`).

## Future work

- **Upgrade `deploy.sh`** to declare the services' full runtime shape (next
  build task): capture the live config of `webhook-handler` and `lpg-admin`
  (`gcloud run services describe`), encode it into the deploy command, and
  verify a redeploy is a no-op against the live resource.
- Revisit the Terraform-with-`ignore_changes` alternative if a CI pipeline or
  team makes declarative drift detection on service config worth the second model.

## References

- Deploy scripts: [`scripts/deploy.sh`](../../scripts/deploy.sh) (services),
  [`scripts/deploy-job.sh`](../../scripts/deploy-job.sh) (job — the full-shape pattern to mirror)
- Related ADRs:
  [ADR-0011](./0011-cloud-run-deploy-architecture.md) (Cloud Run deploy architecture),
  [ADR-0015](./0015-split-webhook-and-admin-services.md) (service split),
  [ADR-0019](./0019-terraform-foundation-and-import-deferral.md) (Terraform boundary; deferred this fork)
