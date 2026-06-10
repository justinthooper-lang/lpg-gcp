# Remote state in GCS: durable, versioned, lockable, and not tied to any
# one laptop. The bucket (lpg-dev-496820-tfstate) was created out-of-band
# with versioning enabled — a backend can't create its own state bucket.
terraform {
  backend "gcs" {
    bucket = "lpg-dev-496820-tfstate"
    prefix = "terraform/state"
  }
}