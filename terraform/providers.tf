# Google provider configuration. project and region are declared as
# variables (see variables.tf) so the same config can target a different
# project/region — the transferability the reference architecture wants.
provider "google" {
  project = var.project_id
  region  = var.region
}