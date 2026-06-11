# Purchase-order PDF storage.
#
# First real resource managed by the Terraform foundation (which until now held
# only backend/provider/variables — ADR-0018 build step 8: "new infra written in
# Terraform from birth"). The bucket is an *immutable audit record* of the exact
# PDF sent to the vendor: PDFs otherwise render on demand from DB rows, but the
# sent artifact must be frozen (the render date drifts, and renderer changes could
# alter output). Each send writes a uniquely-named object, so history is complete.

resource "google_storage_bucket" "purchase_orders" {
  name     = "${var.project_id}-purchase-orders"
  location = var.region

  # Security posture a review expects: no ACLs, no public exposure, and the
  # audit bucket can't be torn down by a stray `terraform destroy`.
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false

  # Versioning preserves prior bytes even if an object name is ever reused —
  # an audit store should never silently lose a record.
  versioning {
    enabled = true
  }
}

# The lpg-admin runtime SA may *create* PO PDFs but not delete or overwrite them
# (create-only; each send writes a uniquely-named object). Bucket-scoped, not
# project-wide — least privilege.
resource "google_storage_bucket_iam_member" "po_pdf_writer" {
  bucket = google_storage_bucket.purchase_orders.name
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:${var.cloud_run_service_account}"
}

# Read access so the service can serve archived PDFs back when asked.
resource "google_storage_bucket_iam_member" "po_pdf_reader" {
  bucket = google_storage_bucket.purchase_orders.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${var.cloud_run_service_account}"
}
