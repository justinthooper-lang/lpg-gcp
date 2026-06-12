# Artifact Registry repository for LPG container images.
#
# Created imperatively on 2026-06-01 (see ADR-0011), brought under Terraform via
# an import block per the import-deferral strategy (ADR-0019). Stable and
# low-churn, which is why it's a low-risk first import. The image *tags* still
# come from the deploy scripts; Terraform manages only the repository resource.
#
# Workflow: `terraform plan` should report "1 to import, 0 to add/change/destroy".
# Any proposed change means the HCL below doesn't match the live repo — fix it
# before applying. After a successful apply, the import block can be removed
# (the resource stays in state); leaving it is harmless but conventionally tidied.


resource "google_artifact_registry_repository" "images" {
  location      = var.region
  repository_id = "lpg-images"
  format        = "DOCKER"
  mode          = "STANDARD_REPOSITORY"
  description   = "LPG application container images"
}
