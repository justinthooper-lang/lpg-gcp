# ADR-0013: URL token authentication for Shift4 webhooks

**Status:** Accepted
**Date:** 2026-06-02
**Supersedes:** the HMAC verification approach in
[ADR-0011](./0011-cloud-run-deploy-architecture.md) (Layer 4a) for
incoming Shift4 webhooks specifically.

## Context

In Layer 4a we implemented HMAC-SHA256 signature verification for
incoming webhooks, assuming Shift4 would sign payloads with a shared
secret per standard webhook security practice (Stripe, GitHub, etc.
all do this).

When we set up a real Shift4Shop webhook on 2026-06-02 and inspected
the admin form (Modules → Webhooks → Add New Webhook), it became
clear the platform does not support signed webhooks. The webhook
configuration form has exactly four fields:

- Webhook Name (label only)
- Webhook URL
- Format (JSON or XML)
- Event (Customer New / Order New / etc.)
- Enabled (checkbox)

There is no shared-secret field. No custom-header support. No Basic
Auth credential. We confirmed this by capturing a real Shift4 webhook
to `webhook.site` and inspecting the headers — only standard headers
present (`user-agent`, `content-type`, `host`), no signature header.

This means our HMAC verification layer would always reject real
Shift4 webhooks because there is no signature to verify against. We
need a different authentication mechanism that works within Shift4's
constraints.

## Decision

The webhook endpoint authenticates inbound requests via a **URL
query-parameter token**. The webhook URL configured in Shift4 admin
takes the form:

```
https://<cloud-run-url>/webhooks/shift4/order-created?token=<TOKEN>
```

The handler reads the token from the query string, compares against
the configured `SHIFT4_WEBHOOK_TOKEN` env var (mounted from Secret
Manager in production) using constant-time equality, and returns 401
if absent or mismatched.

If the env var is unset, verification is bypassed — useful for local
dev where `curl` doesn't need to include a token. Production must
always set the token.

### Implementation

- `webhook-handler/auth.py`: `verify_token(received: str | None) -> bool`
- `webhook-handler/main.py`: route handler reads
  `request.query_params.get("token")`, calls `verify_token`, returns
  401 if False
- `shift4-webhook-token` secret in Secret Manager (currently version 2;
  version 1 was rotated out after being leaked to chat during
  development on 2026-06-02)
- Cloud Run service mounts the secret as `SHIFT4_WEBHOOK_TOKEN` env
  var at `:latest`

### Token generation and rotation

Generate a fresh random token:

```bash
openssl rand -hex 24  # 48-char hex token
```

Add a new version to Secret Manager and trigger a Cloud Run revision:

```bash
NEW_TOKEN=$(openssl rand -hex 24)
printf '%s' "$NEW_TOKEN" | gcloud secrets versions add shift4-webhook-token --data-file=-
unset NEW_TOKEN
gcloud secrets versions disable <previous-version> --secret=shift4-webhook-token
gcloud run services update webhook-handler --region=us-west1 \
  --update-secrets=SHIFT4_WEBHOOK_TOKEN=shift4-webhook-token:latest
# Then update the URL in Shift4 admin with the new token.
```

To get the production URL onto the clipboard without echoing the token:

```bash
printf 'https://webhook-handler-388123220900.us-west1.run.app/webhooks/shift4/order-created?token=%s' \
  "$(gcloud secrets versions access latest --secret=shift4-webhook-token)" | pbcopy
```

## Alternatives considered

**HMAC-SHA256 signature in a custom request header.** Rejected:
Shift4's webhook form has no place to configure custom headers, and
no shared-secret field. We can't make Shift4 send something the
platform doesn't expose.

**Source-IP filtering.** Rejected: Shift4 doesn't document their
egress IP range, and the source we observed (`20.119.161.4`) is an
Azure-hosted range that almost certainly rotates. Any allow-list we
built would silently break.

**OAuth / OpenID Connect.** Rejected: not supported by Shift4's
webhook delivery mechanism.

**No authentication at all.** Rejected: the endpoint URL would
effectively be the only secret, and lacking any authentication on
the request means anyone who learns the URL through any channel
(logs, mistaken sharing) can inject arbitrary orders into our DB.

**Cloud Armor IP allow-list.** Considered for future work. Could
layer below this as defense-in-depth once we know Shift4's actual
egress IP ranges. Not blocking for the initial deploy.

## Consequences

**Positive:**

- The endpoint authenticates correctly with real Shift4 webhooks.
- Token verification is constant-time (`hmac.compare_digest`); no
  timing side-channel.
- Rotation is two commands plus a Shift4 admin paste.
- Falls back to "dev mode" (no token configured) cleanly so local
  testing with curl still works without the token.

**Negative — security trade-off vs HMAC:**

- With HMAC, an attacker who learned the URL still could not forge
  a valid request without knowing the shared secret. With URL
  tokens, the URL itself contains the credential. Anyone who
  observes the URL learns enough to forge requests.
- The URL with token traverses: Shift4Shop admin UI (visible to
  anyone with admin access), TLS connections (encrypted in transit,
  may appear in TLS-termination logs at various hops), Shift4's
  internal webhook-delivery infrastructure (unknown logging
  behavior), and our own Cloud Run access logs (query string
  appears in default access log format).
- We are relying on Shift4Shop to keep the URL confidential. For
  LPG's risk profile this is acceptable. For higher-stakes systems
  it would not be.
- Token compromise requires URL replacement in Shift4 admin in
  addition to secret rotation. Slightly higher operational cost
  than HMAC rotation, which is purely server-side.

**Negative — code cleanliness:**

- We carry HMAC code (`smoke_auth.py`) in the repo that no longer
  runs in production. Kept as a reference and for the eventual
  case where we ingest webhooks from a system that does sign them
  (an actual ERP integration, a payment gateway, etc.).

## Future work

- **Cloud Armor for source-IP allow-listing**, once Shift4 publishes
  or we empirically determine their stable egress range. Adds
  defense-in-depth.
- **Cloud Run access log scrubbing** to redact `?token=*` from query
  strings in Cloud Logging. The token already appears in
  `request_started` events via the path, so this is partially
  defeated until we also strip the query in our middleware.
- **Move to a webhook-delivery service** (e.g., Hookdeck, Svix) that
  can re-sign requests with HMAC before forwarding to our endpoint.
  Adds latency and a third-party dependency but restores the HMAC
  threat model.

## References

- Implementation: [`webhook-handler/auth.py`](../../webhook-handler/auth.py)
- Token secret: `shift4-webhook-token` in Secret Manager
- Related ADRs:
  [ADR-0011](./0011-cloud-run-deploy-architecture.md) (deploy arch,
  Layer 4a HMAC initially),
  [ADR-0012](./0012-iam-database-auth.md) (IAM DB auth pattern)
