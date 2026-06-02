"""HMAC signature verification for Shift4 webhooks."""

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
    """Verify a webhook signature against the configured secret."""
    secret = webhook_secret()
    if secret is None:
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
