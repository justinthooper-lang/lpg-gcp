"""HMAC signature verification for Shift4 webhooks.

Shift4 (per their documented webhook security pattern) signs each
request body with an HMAC-SHA256 over the raw body using a shared
secret. The signature arrives in a request header. We re-compute the
HMAC server-side and compare with constant-time equality.

The exact header name and signature encoding (hex vs base64) Shift4
uses will be finalized when we connect a real webhook in production.
For now we assume:

  - Header:  X-Shift4-Signature
  - Format:  hex-encoded HMAC-SHA256(body, secret)

The shared secret is read from the SHIFT4_WEBHOOK_SECRET environment
variable. If unset, signature verification is disabled (useful for
local dev with curl; will be set in Cloud Run via Secret Manager).
"""

from __future__ import annotations

import hmac
import hashlib
import os

import structlog

log = structlog.get_logger()

SIGNATURE_HEADER = "x-shift4-signature"


def webhook_secret() -> str | None:
    """Return the configured webhook secret, or None if unset."""
    secret = os.getenv("SHIFT4_WEBHOOK_SECRET")
    return secret if secret else None


def verify_signature(body: bytes, signature_header: str | None) -> bool:
    """Verify a webhook signature against the configured secret.

    Returns True if:
      - No secret is configured (dev mode), OR
      - The signature header matches HMAC-SHA256(body, secret)

    Returns False if a secret is configured but the signature is
    absent, malformed, or mismatched.
    """
    secret = webhook_secret()
    if secret is None:
        # Dev mode — accept without checking. Production must always set this.
        log.warning("webhook_signature_skipped_no_secret_configured")
        return True

    if not signature_header:
        log.warning("webhook_signature_missing")
        return False

    expected = hmac.new(
        secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()

    if hmac.compare_digest(expected, signature_header.strip().lower()):
        return True

    log.warning("webhook_signature_mismatch")
    return False
    