"""Sync Crown Plastics invoices from Outlook to lpg.vendor_invoices.

Production: runs as a Cloud Run job, scheduled daily by Cloud Scheduler.
Local dev: run with env vars set, against the local Cloud SQL Proxy.

Pipeline per message:
    Graph fetch PDF bytes -> parse_crown_invoice() -> write_crown_invoice()

Crown sends two identical emails per invoice; dedup on invoice number
(in the writer) makes that a no-op. Order Confirmations are excluded by
the subject filter and, defensively, rejected by the parser's guard.

Required env vars:
    AZURE_TENANT_ID
    AZURE_CLIENT_ID
    AZURE_CLIENT_SECRET
    TARGET_MAILBOX        (e.g., customerservice@lamppostglobes.com)

See ADR-0016 for design.
"""

from __future__ import annotations

import base64
import os
import sys

import requests
from msal import ConfidentialClientApplication

GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# crown_invoice_parser / _writer are siblings in this dir (not yet packaged).
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

from crown_invoice_parser import (  # noqa: E402
    InvoiceReconcileError,
    NotAnInvoiceError,
    parse_crown_invoice,
)
from crown_invoice_writer import write_crown_invoice  # noqa: E402
from lpg_common.db import get_connection  # noqa: E402

CROWN_VENDOR_CODE = "CROWN"


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        print(f"ERROR: missing env var {name}", file=sys.stderr)
        sys.exit(1)
    return value


def get_access_token() -> str:
    """Acquire a Graph access token via client-credentials flow."""
    tenant_id = _require_env("AZURE_TENANT_ID")
    client_id = _require_env("AZURE_CLIENT_ID")
    client_secret = _require_env("AZURE_CLIENT_SECRET")

    app = ConfidentialClientApplication(
        client_id=client_id,
        client_credential=client_secret,
        authority=f"https://login.microsoftonline.com/{tenant_id}",
    )
    result = app.acquire_token_for_client(
        scopes=["https://graph.microsoft.com/.default"]
    )
    if "access_token" not in result:
        print(f"ERROR: token acquisition failed: {result}", file=sys.stderr)
        sys.exit(1)
    return result["access_token"]


# --- Crown invoice identification ----------------------------------------
# A forwarded email's From is rewritten to the forwarding mailbox, so we
# must NOT key on sender. What survives a forward intact is the subject
# signature and the invoice PDF attachment. See ADR-0016 / ADR-0017.
CROWN_SUBJECT_MARKERS = ("invoice/tracking information", "crown plastics")
CROWN_ATTACHMENT_PREFIX = "invoice_"


def _is_crown_invoice(msg: dict) -> bool:
    """True if a message looks like a Crown Plastics invoice."""
    subject = (msg.get("subject") or "").lower()
    if not all(marker in subject for marker in CROWN_SUBJECT_MARKERS):
        return False
    for att in msg.get("attachments", []):
        name = (att.get("name") or "").lower()
        if name.startswith(CROWN_ATTACHMENT_PREFIX) and name.endswith(".pdf"):
            return True
    return False


def fetch_crown_messages(token: str, mailbox: str, limit: int = 50) -> list[dict]:
    """Fetch recent Crown invoice messages, newest first.

    Pulls recent messages with attachments expanded, then filters
    client-side via `_is_crown_invoice`.
    """
    url = (
        f"{GRAPH_BASE}/users/{mailbox}/messages"
        f"?$expand=attachments"
        f"&$orderby=receivedDateTime desc"
        f"&$top=50"
        f"&$select=id,subject,from,receivedDateTime,hasAttachments"
    )
    response = requests.get(
        url, headers={"Authorization": f"Bearer {token}"}, timeout=30,
    )
    response.raise_for_status()
    all_messages = response.json().get("value", [])
    crown_messages = [m for m in all_messages if _is_crown_invoice(m)]
    return crown_messages[:limit]


def _pdf_attachment(msg: dict) -> dict | None:
    """Return the invoice PDF attachment object for a message, if any."""
    for att in msg.get("attachments", []):
        name = (att.get("name") or "").lower()
        if name.startswith(CROWN_ATTACHMENT_PREFIX) and name.endswith(".pdf"):
            return att
    return None


def get_pdf_bytes(token: str, mailbox: str, msg: dict, att: dict) -> bytes:
    """Return the attachment's raw bytes.

    Prefers the inline base64 contentBytes from the $expand fetch; falls
    back to an explicit per-attachment $value call if it's absent.
    """
    content_b64 = att.get("contentBytes")
    if content_b64:
        return base64.b64decode(content_b64)
    url = (
        f"{GRAPH_BASE}/users/{mailbox}/messages/{msg['id']}"
        f"/attachments/{att['id']}/$value"
    )
    r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=60)
    r.raise_for_status()
    return r.content


def get_crown_vendor_id(conn) -> int:
    cur = conn.cursor()
    cur.execute(
        "SELECT vendor_id FROM lpg.vendors WHERE vendor_code = %s",
        (CROWN_VENDOR_CODE,),
    )
    row = cur.fetchone()
    if row is None:
        raise RuntimeError(
            f"Vendor row not found (vendor_code={CROWN_VENDOR_CODE!r})"
        )
    return row[0]


def main() -> int:
    print("Acquiring access token...")
    token = get_access_token()

    mailbox = _require_env("TARGET_MAILBOX")
    print(f"Fetching Crown invoice messages from {mailbox}...")
    messages = fetch_crown_messages(token, mailbox)
    print(f"  {len(messages)} Crown message(s) to process.\n")

    with get_connection() as conn:
        crown_vendor_id = get_crown_vendor_id(conn)

    counts = {"ingested": 0, "duplicate": 0, "not_invoice": 0,
              "reconcile_failed": 0, "error": 0}

    for i, msg in enumerate(messages, start=1):
        subject = msg.get("subject", "")
        att = _pdf_attachment(msg)
        if att is None:
            print(f"  [{i}] no invoice PDF, skipping: {subject}")
            counts["not_invoice"] += 1
            continue

        try:
            pdf_bytes = get_pdf_bytes(token, mailbox, msg, att)
            parsed = parse_crown_invoice(pdf_bytes)
        except NotAnInvoiceError as e:
            print(f"  [{i}] SKIP not-an-invoice: {e}")
            counts["not_invoice"] += 1
            continue
        except InvoiceReconcileError as e:
            print(f"  [{i}] RECONCILE FAILURE ({att.get('name')}): {e}",
                  file=sys.stderr)
            counts["reconcile_failed"] += 1
            continue
        except Exception as e:  # noqa: BLE001
            print(f"  [{i}] ERROR parsing {att.get('name')}: {e}",
                  file=sys.stderr)
            counts["error"] += 1
            continue

        try:
            with get_connection() as conn:
                result = write_crown_invoice(
                    conn,
                    parsed,
                    vendor_id=crown_vendor_id,
                    graph_message_id=msg["id"],
                    raw_pdf_filename=att.get("name"),
                )
        except Exception as e:  # noqa: BLE001
            print(f"  [{i}] ERROR writing invoice "
                  f"{parsed.get('invoice_number')}: {e}", file=sys.stderr)
            counts["error"] += 1
            continue

        if result.inserted:
            print(f"  [{i}] ingested invoice {parsed['invoice_number']} "
                  f"(PO {parsed.get('customer_po_number')}, "
                  f"{result.line_count} lines)")
            counts["ingested"] += 1
        else:
            print(f"  [{i}] duplicate, skipped: invoice "
                  f"{parsed['invoice_number']}")
            counts["duplicate"] += 1

    print("\nSummary:")
    for k in ("ingested", "duplicate", "not_invoice", "reconcile_failed", "error"):
        print(f"  {k}: {counts[k]}")

    # Non-zero exit so Cloud Scheduler/monitoring flags runs needing attention.
    return 1 if (counts["reconcile_failed"] or counts["error"]) else 0


if __name__ == "__main__":
    sys.exit(main())