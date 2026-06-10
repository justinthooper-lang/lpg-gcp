# Pin Terraform and provider versions for reproducibility.
# A client cloning this repo gets the same toolchain we validated against.
terraform {
  required_version = ">= 1.15.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
}