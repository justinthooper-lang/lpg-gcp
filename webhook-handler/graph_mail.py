"""Microsoft Graph Mail.Send client for emailing POs to Crown (ADR-0018).

Uses a **separate send-only Azure app** (ADR-0018 Q1) — distinct from the read-only
crown-sync app — so the least-privilege boundary from ADR-0017 holds: the read app
can only read mail, the send app can only send it. Both share the tenant; the send
app holds Mail.Send Application permission scoped to a single mailbox via its own
Application Access Policy.

Token acquisition mirrors scripts/sync_crown_invoices.py (MSAL client-credentials),
but reads the send app's own credentials:
    AZURE_TENANT_ID         (shared tenant)
    AZURE_SEND_CLIENT_ID    (the send-only app)
    AZURE_SEND_CLIENT_SECRET
    CROWN_PO_MAILBOX        (the mailbox the PO is sent *from*; the app is scoped to it)

The payload builder is pure and unit-tested; token acquisition and the HTTP POST are
thin wrappers mocked in tests.
"""

from __future__ import annotations

import base64
import os

import requests
from msal import ConfidentialClientApplication

GRAPH_BASE = "https://graph.microsoft.com/v1.0"


class GraphSendError(Exception):
    """Raised when sending mail via Graph fails."""


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise GraphSendError(f"missing env var {name}")
    return value


def acquire_send_token() -> str:
    """Acquire a Graph token for the send-only app via client-credentials."""
    tenant_id = _require_env("AZURE_TENANT_ID")
    client_id = _require_env("AZURE_SEND_CLIENT_ID")
    client_secret = _require_env("AZURE_SEND_CLIENT_SECRET")

    app = ConfidentialClientApplication(
        client_id=client_id,
        client_credential=client_secret,
        authority=f"https://login.microsoftonline.com/{tenant_id}",
    )
    result = app.acquire_token_for_client(
        scopes=["https://graph.microsoft.com/.default"]
    )
    if "access_token" not in result:
        raise GraphSendError(
            f"token acquisition failed: "
            f"{result.get('error_description', result)}"
        )
    return result["access_token"]


def build_send_mail_payload(
    *,
    recipient: str,
    subject: str,
    body_text: str,
    attachment_name: str,
    attachment_bytes: bytes,
) -> dict:
    """Build the Graph sendMail request body with one PDF file attachment (pure)."""
    return {
        "message": {
            "subject": subject,
            "body": {"contentType": "Text", "content": body_text},
            "toRecipients": [{"emailAddress": {"address": recipient}}],
            "attachments": [
                {
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": attachment_name,
                    "contentType": "application/pdf",
                    "contentBytes": base64.b64encode(attachment_bytes).decode("ascii"),
                }
            ],
        },
        "saveToSentItems": True,
    }


def send_mail(token: str, *, mailbox: str, payload: dict) -> None:
    """POST the sendMail request. Graph returns 202 Accepted on success."""
    url = f"{GRAPH_BASE}/users/{mailbox}/sendMail"
    resp = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=60,
    )
    if resp.status_code != 202:
        raise GraphSendError(
            f"sendMail failed: HTTP {resp.status_code} {resp.text[:300]}"
        )


def send_purchase_order_email(
    *,
    recipient: str,
    po_number: str,
    pdf_bytes: bytes,
    body_text: str | None = None,
) -> str:
    """Email the PO PDF to ``recipient`` from the configured mailbox.

    Returns the mailbox it was sent from. Raises GraphSendError on any failure.
    """
    mailbox = _require_env("CROWN_PO_MAILBOX")
    subject = f"Purchase Order {po_number} \u2014 Lamp Post Globes"
    body = body_text or (
        f"Hello,\n\nPlease find attached purchase order {po_number} from "
        f"Lamp Post Globes.\n\nThank you,\nLamp Post Globes"
    )
    payload = build_send_mail_payload(
        recipient=recipient,
        subject=subject,
        body_text=body,
        attachment_name=f"{po_number}.pdf",
        attachment_bytes=pdf_bytes,
    )
    token = acquire_send_token()
    send_mail(token, mailbox=mailbox, payload=payload)
    return mailbox
