output "purchase_orders_bucket" {
  description = "GCS bucket holding generated purchase-order PDFs."
  value       = google_storage_bucket.purchase_orders.name
}
