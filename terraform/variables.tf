# Input variables. Defaults target the current LPG dev project, but every
# value is overridable — point this config at another project/region by
# setting these (CLI -var, a .tfvars file, or env vars) without editing code.

variable "project_id" {
  description = "GCP project ID for all resources."
  type        = string
  default     = "lpg-dev-496820"
}

variable "region" {
  description = "Primary GCP region for regional resources."
  type        = string
  default     = "us-west1"
}