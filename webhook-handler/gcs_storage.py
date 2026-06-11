"""GCS archival of sent purchase-order PDFs (ADR-0018 Q5 / build step 7).

The PO PDF normally renders on demand from DB rows. When a PO is *sent*, though,
the exact bytes that went to the vendor are archived to GCS as an immutable record
for the three-way match — frozen against renderer changes and the drifting render
date. Each send writes a uniquely-named object, so the bucket holds the full history.

The bucket is Terraform-managed (`terraform/storage.tf`); the runtime SA has
bucket-scoped `objectCreator`/`objectViewer`. Bucket name comes from `PO_PDF_BUCKET`.
On Cloud Run the client authenticates via Application Default Credentials (the
compute SA) with no key material.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from google.cloud import storage

_client: storage.Client | None = None


class GcsStorageError(Exception):
    """Raised when archiving a PO PDF to GCS fails."""


def _get_client() -> storage.Client:
    global _client
    if _client is None:
        _client = storage.Client()
    return _client


def build_object_name(po_number: str, *, now: datetime | None = None) -> str:
    """Unique, sortable object path for one send: never overwrites a prior send."""
    now = now or datetime.now(timezone.utc)
    ts = now.strftime("%Y%m%dT%H%M%SZ")
    return f"purchase-orders/{po_number}/{po_number}-{ts}.pdf"


def upload_po_pdf(
    po_number: str,
    pdf_bytes: bytes,
    *,
    now: datetime | None = None,
) -> str:
    """Upload the sent PDF and return its ``gs://`` URI. Raises GcsStorageError."""
    bucket_name = os.getenv("PO_PDF_BUCKET")
    if not bucket_name:
        raise GcsStorageError("missing env var PO_PDF_BUCKET")
    name = build_object_name(po_number, now=now)
    try:
        bucket = _get_client().bucket(bucket_name)
        blob = bucket.blob(name)
        blob.upload_from_string(pdf_bytes, content_type="application/pdf")
    except Exception as exc:  # noqa: BLE001 — surface any client/transport error uniformly
        raise GcsStorageError(f"upload failed: {exc}") from exc
    return f"gs://{bucket_name}/{name}"
