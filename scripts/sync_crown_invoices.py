"""Sync Crown Plastics invoices from Outlook to lpg.vendor_invoices.

Production: runs as a Cloud Run job, scheduled daily by Cloud Scheduler.
Local dev: run with env vars set, against the local Cloud SQL Proxy.

This is checkpoint 1: OAuth + "who can we see" Graph call only.
Actual message fetching, PDF parsing, and DB writes come next.

Required env vars:
    AZURE_TENANT_ID
    AZURE_CLIENT_ID
    AZURE_CLIENT_SECRET
    TARGET_MAILBOX        (e.g., customerservice@lamppostglobes.com)

See ADR-0016 for design.
"""

from __future__ import annotations

import os
import sys

import requests
from msal import ConfidentialClientApplication

GRAPH_BASE = "https://graph.microsoft.com/v1.0"


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

    # ".default" requests all the *application* permissions Azure has
    # granted to this app — for us, just Mail.Read (Application).
    # This is the right scope for client-credentials flow; you don't
    # request individual scopes the way delegated flow does.
    result = app.acquire_token_for_client(
        scopes=["https://graph.microsoft.com/.default"]
    )

    if "access_token" not in result:
        print(f"ERROR: token acquisition failed: {result}", file=sys.stderr)
        sys.exit(1)

    return result["access_token"]


def main() -> int:
    print("Acquiring access token...")
    token = get_access_token()
    print(f"  Token acquired ({len(token)} chars)")

    mailbox = _require_env("TARGET_MAILBOX")
    print(f"\nFetching recent Crown messages from {mailbox}...")
    messages = fetch_crown_messages(token, mailbox, limit=5)
    print(f"  Found {len(messages)} message(s).\n")

    for i, msg in enumerate(messages, start=1):
        attachments = msg.get("attachments", [])
        pdf_attachments = [
            a for a in attachments
            if a.get("name", "").lower().endswith(".pdf")
        ]
        print(f"  [{i}] {msg.get('receivedDateTime')}")
        print(f"      subject: {msg.get('subject')}")
        print(f"      attachments: {len(attachments)} total, {len(pdf_attachments)} PDF")
        for a in pdf_attachments:
            size_kb = a.get("size", 0) / 1024
            print(f"        - {a.get('name')} ({size_kb:.1f} KB)")

    return 0

def fetch_crown_messages(token: str, mailbox: str, limit: int = 5) -> list[dict]:
    """Fetch up to `limit` recent messages from Crown.

    Uses $filter to scope to crown@plasticglobes.com and $expand to
    pull attachments inline (one round trip per message instead of two).
    Newest first.
    """
    # Graph's $filter on from/emailAddress/address has finicky restrictions
    # when combined with $orderby and $expand. Simpler: grab recent
    # messages, filter client-side. For our scale (handful of Crown
    # messages per week), this is plenty efficient.
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

    # Client-side filter to Crown sender.
    crown_messages = [
        m for m in all_messages
        if m.get("from", {}).get("emailAddress", {}).get("address", "").lower()
        == "crown@plasticglobes.com"
    ]

    return crown_messages[:limit]
    
if __name__ == "__main__":
    sys.exit(main())