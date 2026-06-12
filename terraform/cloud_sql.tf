# Cloud SQL Postgres instance — the system's database (ADR-0008).
#
# Created imperatively on 2026-05-28; brought under Terraform via an import block
# per ADR-0019. This instance holds real data, so the import is plan-gated and
# `deletion_protection = true` guards against `terraform destroy`.
#
# Lean by design: only required fields + the few that differ from provider
# defaults are declared; everything else (disk size with autoresize, backup and
# IP blocks, location preference) is left to compute from the live instance to
# avoid spurious diffs. `terraform plan` must show "1 to import, 0 to change,
# 0 to destroy" before apply — any proposed change means a field needs to match
# reality, and any proposed *replacement* is an immediate stop.

import {
  to = google_sql_database_instance.lpg_dev
  id = "${var.project_id}/lpg-dev"
}

resource "google_sql_database_instance" "lpg_dev" {
  name             = "lpg-dev"
  database_version = "POSTGRES_16"
  region           = var.region

  # Terraform-side guard (not an API field); prevents accidental destroy.
  deletion_protection = true

  settings {
    tier    = "db-f1-micro"
    edition = "ENTERPRISE"

    # Reality has this on; provider default is off, so it must be declared.
    enable_dataplex_integration = true

    # IAM database authentication (ADR-0012). Must be declared, or Terraform
    # would propose removing the flag and break IAM DB auth.
    database_flags {
      name  = "cloudsql.iam_authentication"
      value = "on"
    }
  }
}
