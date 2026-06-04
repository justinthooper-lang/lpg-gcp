"""URL token verification for Shift4 webhooks.

Shift4Shop's webhook configuration UI does not support HMAC signing,
custom request headers, or Basic Auth (verified via the live webhook
admin form on 2026-06-02). The only knob we have is the webhook URL
itself.

We embed a long random token as a query parameter:

    https://.../webhooks/shift4/order-created?token=<TOKEN>

The handler reads the SHIFT4_WEBHOOK_TOKEN env var (mounted from
Secret Manager in production) and compares against the incoming
`token` query parameter using constant-time equality.

If SHIFT4_WEBHOOK_TOKEN is unset, verification is bypassed (useful for
local dev). Production must always set it. See ADR-0013.
"""

from __future__ import annotations

import hmac
import os

import structlog

log = structlog.get_logger()


def webhook_token() -> str | None:
    """Return the configured token, or None if unset."""
    token = os.getenv("SHIFT4_WEBHOOK_TOKEN")
    return token if token else None


def verify_token(received_token: str | None) -> bool:
    """Verify a webhook token against the configured value.

    Returns True if no token is configured (dev mode), OR if the
    received token matches SHIFT4_WEBHOOK_TOKEN.

    Returns False if a token is configured but the received value
    is absent or mismatched.
    """
    expected = webhook_token()
    if expected is None:
        log.warning("webhook_token_skipped_no_token_configured")
        return True

    if not received_token:
        log.warning("webhook_token_missing")
        return False

    if hmac.compare_digest(expected, received_token):
        return True

    log.warning("webhook_token_mismatch")
    return False


def is_admin_service() -> bool:
    """True if this process is running as the IAM-protected admin service.

    Cloud Run sets K_SERVICE to the service name. When K_SERVICE is
    'lpg-admin', the Google frontend has already verified the caller's
    identity via IAM before the request reached us, so the URL-token
    check becomes redundant and can be skipped.

    For any other K_SERVICE value (e.g. 'webhook-handler') or when
    K_SERVICE is unset (local dev), this returns False and callers
    should still verify the URL token.
    """
    return os.getenv("K_SERVICE") == "lpg-admin"


def is_authorized_read(received_token: str | None) -> bool:
    """Authorization gate for read endpoints.

    Read endpoints are dual-served: on `lpg-admin` (IAM-protected) and
    on `webhook-handler` (public, URL-token protected). This helper
    centralizes the per-service decision so route handlers don't have
    to care which service they're running on.
    """
    if is_admin_service():
        return True
    return verify_token(received_token)
