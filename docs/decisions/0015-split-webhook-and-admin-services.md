# ADR-0015: Split webhook-handler and lpg-admin into separate Cloud Run services

**Status:** Accepted
**Date:** 2026-06-04

## Context

The webhook-handler service started with a single responsibility:
receive Shift4 order webhooks and write them to Cloud SQL. URL-token
authentication (ADR-0013) made sense because Shift4 has no way to
mint Google identity tokens — the token in the query string was the
only auth knob available.

We then added read endpoints (`/orders`, `/orders.html`,
`/orders/{id}`, `/orders/{id}.html`) for human inspection of the
data. These reused the same URL-token mechanism. That worked for
proving the schema but had a real security problem: the token ended
up in browser history, browser address bar, screen shares, and any
HTTP access logs that captured query strings. A token in a URL is
not a credential anymore; it's a screenshot waiting to happen.

The webhook endpoint genuinely cannot move off URL-token auth —
Shift4Shop's webhook configuration UI doesn't expose any other
mechanism (verified again on 2026-06-04). So we have two different
clients with two different acceptable auth stories:

- **Shift4** (machine, no Google identity) — needs URL token
- **Justin in a browser, or curl from a terminal** (human with a
  Google account) — should use IAM

These don't belong in the same service. They have different threat
surfaces, different observability needs, and different scaling
profiles. This ADR splits them.

## Decision

**Two Cloud Run services, both from the same container image:**

1. **`webhook-handler`** — public (`--allow-unauthenticated`). Serves
   the Shift4 webhook endpoint. Still serves the read endpoints too,
   for now, behind the URL token; those will be removed in a
   follow-up commit so this service has a single job.
2. **`lpg-admin`** — IAM-protected (`--no-allow-unauthenticated`).
   Google's frontend rejects unauthenticated requests with 403 before
   they reach the container. Caller must present a valid OAuth ID
   token via `Authorization: Bearer ...`.

**Per-service auth decision lives in code, keyed off `K_SERVICE`.**
Cloud Run sets `K_SERVICE` to the service name at runtime. The
shared image has one helper:

```python
def is_admin_service() -> bool:
    return os.getenv("K_SERVICE") == "lpg-admin"
```

Read endpoints call `is_authorized_read()` instead of `verify_token()`.
That helper returns `True` immediately when running as `lpg-admin`
(the frontend already authenticated the caller), and falls back to
URL-token verification otherwise. The webhook endpoint still calls
`verify_token()` directly because it must always require the token.

**Browser workflow uses `gcloud run services proxy`.** Browsers
don't natively add `Authorization` headers, so a vanilla browser
hitting `https://lpg-admin-...` gets 403. The proxy runs a local
HTTP server on `127.0.0.1:8080` that attaches the IAM token to
every forwarded request. The browser hits localhost; the proxy
handles the auth.

Start it with:

```
gcloud run services proxy lpg-admin --region=us-west1
```

The terminal session must stay open while browsing. Token refresh
is automatic.

## Alternatives considered

**One service, per-path JWT verification in code.** Keep
`webhook-handler` public, write code that detects an
`Authorization: Bearer` header on read endpoints and verifies the
JWT against Google's JWKS. Rejected — JWT verification is annoying
to get right (key rotation, JWKS caching, audience checks), and
the resulting trust model isn't visible from `gcloud run services
list`. Cloud Run's own IAM check is the well-trodden path.

**One service, drop URL-token auth from read endpoints and trust
IAM.** Can't — the service is `--allow-unauthenticated` to keep
Shift4 working, so anyone with the public URL would be able to read
order data. Mixing public webhook + private reads on the same service
isn't possible at the Cloud Run frontend layer.

**Use Identity-Aware Proxy (IAP) in front of Cloud Run.** Heavier
infrastructure than needed at our scale. Worth revisiting if we
later have multiple admin services and want a single sign-on
surface, or if non-Google users (a contractor with a Cloud Identity)
need access.

**Build a browser extension for the auth header.** A maintenance
burden for a one-person project. The `gcloud run services proxy`
approach uses already-installed tooling.

## Consequences

**Positive:**

- Browser address bar contains no credentials. URL is
  `127.0.0.1:8080/orders.html`. Screen sharing is safe; browser
  history is safe.
- Trust model is visible from `gcloud run services list` — one
  public, one private. No code reading needed to understand who's
  allowed where.
- Shift4 webhook auth is unchanged. No coordination with Shift4 was
  required to ship this.
- Two services, same image: deployments stay in sync naturally
  because we build once and deploy twice. No risk of one service
  drifting from the other on a code change.

**Negative:**

- Two `gcloud run deploy` calls per release instead of one. Mitigated
  by scripting once we have more than a few releases per week.
- `gcloud run services proxy` must be running for browser access.
  That's a third local terminal-tab dependency (alongside Cloud SQL
  proxy + uvicorn for local dev, though the admin proxy is unrelated
  to those — it talks to deployed Cloud Run, not local).
- `lpg-admin` currently has a `SHIFT4_WEBHOOK_TOKEN` secret mount it
  doesn't use. Cosmetic; removing it is a follow-up.

## Verification

The auth matrix tested at deploy time on 2026-06-04:

| Service | Auth | Expected | Got |
|---|---|---|---|
| `lpg-admin` | none | 403 (Google frontend) | 403 ✓ |
| `lpg-admin` | IAM token | 200 | 200 ✓ |
| `webhook-handler` | none | 401 (app layer) | 401 ✓ |
| `webhook-handler` | URL token | 200 | 200 ✓ |
| `webhook-handler` | URL token (POST webhook) | 200 or 422 (model validation) | 422 ✓ |

The 422 on the POST test was a malformed payload (InvoiceNumber as
string instead of int); auth passed and Pydantic correctly rejected
the body. Confirmed Shift4's real production webhooks still ingest
correctly.

## Future work

- Remove read endpoints from `webhook-handler`. Once `lpg-admin` is
  the established read path, the parallel auth code on the webhook
  service is dead surface area. Targeting next session.
- Remove the `SHIFT4_WEBHOOK_TOKEN` secret mount from `lpg-admin`.
  Cosmetic; reduces blast radius if the secret leaks.
- Wrap the two `gcloud run deploy` calls in a `scripts/deploy.sh`.
  Right now we type both manually; that's fine for low frequency but
  becomes a footgun once releases speed up.
- Consider Cloud Run jobs (separate from services) for any background
  work that doesn't need to serve HTTP — currently we have none, but
  the seed script could become one if it grows.

## References

- Implementation:
  - [`webhook-handler/auth.py`](../../webhook-handler/auth.py) —
    `is_admin_service()` and `is_authorized_read()`
  - [`webhook-handler/main.py`](../../webhook-handler/main.py) — read
    endpoints call `is_authorized_read`; webhook endpoint calls
    `verify_token` directly
- Related ADRs:
  [ADR-0011](./0011-cloud-run-deploy-architecture.md) (Cloud Run deploy architecture),
  [ADR-0012](./0012-iam-database-auth.md) (IAM database auth on Cloud Run),
  [ADR-0013](./0013-url-token-auth-for-shift4.md) (URL token auth for Shift4)
