# ADR-0026: Security review — findings and hardening

**Status:** Accepted
**Date:** 2026-06-17

## Context

After the margin dashboard and manual-entry features shipped, we ran a full
security review of the LPG-GCP system: the public/admin Cloud Run split,
application auth, secret handling, service-account IAM, the Azure/Graph mail
integration, and PII in the order mirror. The goal was both to button up LPG and
to be able to show a defensible security posture for the consulting reference
architecture. This ADR records what was found, what was changed, and what was
verified clean, so the review is auditable rather than tribal knowledge.

GCP shared-responsibility framing used for the review: Google owns physical/
infra security, encryption at rest + in transit by default, edge DDoS, and
managed-runtime patching. We own IAM, service-account scoping, application auth,
secret management, network exposure, and data governance — so the review focused
on what we own.

## Decision / Findings

### Finding 1 — `/dashboard` was publicly reachable with the webhook token (MEDIUM) — FIXED

The margin dashboard exposed revenue/cost/profit. It was registered with a bare
`@app.get` decorator (so it existed on the public `webhook-handler`) and gated
with `is_authorized_read`, which falls back to the Shift4 URL token on a non-admin
service. Confirmed live: `GET https://webhook-handler…/dashboard?token=<TOKEN>`
returned 200. The Shift4 webhook token is a weak credential for this — it travels
in URL query strings and so lands in access logs, browser history, and the
Shift4 admin config.

**Fix:** registered `/dashboard` only inside the admin-only block
(`if is_admin_service() or _K_SERVICE is None`) and gated it on
`is_admin_service()`. After deploy (v0.24.0), the public path returns 404 and the
IAM-authenticated admin path returns 200 — both verified live.

(Note: the first attempt, v0.23.0, crashed `lpg-admin` on startup with a
`NameError` because the route was registered before `dashboard_html` was defined.
Module-scope forward references aren't caught by an AST parse — only by actually
importing the module. v0.24.0 moved the registration after the definition and was
import-tested under both `K_SERVICE` identities before deploy.)

### Finding 2 — read-auth retained an unused public token fallback (LOW) — HARDENED

`is_authorized_read` returned true if the request carried the valid Shift4 token,
even on a non-admin service. In practice the read routes (`/orders`, order
detail, overrides, margin) are all registered only in the admin-only block, so
the token path was never reachable in production — but it was latent attack
surface and the trap that Finding 1 fell into.

**Fix (v0.25.0):** `is_authorized_read` is now admin-only in production —
true on `lpg-admin` (IAM already authenticated the caller) and in local dev
(`K_SERVICE` unset), false on any other deployed service. The Shift4 URL token is
no longer a read credential anywhere. Verified: admin reads still 200; the
inbound webhook (`verify_token`, unchanged) still 200.

### Verified clean (no change required)

- **PO routes** (generate / line CRUD / pdf / send): each guards internally with
  `is_admin_service()`; not exposed on the public service. Weaker than
  conditional registration (defense-in-depth) but functionally protected.
- **SQL injection:** all values parameterized (`%s`); the two `f"SELECT {cols}…"`
  interpolate a static hardcoded column list, not user input.
- **No hardcoded secrets:** all via `_require_env` / Secret Manager.
- **Service-account IAM:** no broad primitive roles (`editor`/`owner`) on the
  compute or crown-sync SAs; each holds only narrow roles (Cloud SQL client/
  instanceUser, artifact writer, log writer, storage objectViewer). Crown-sync
  has DB access only.
- **Secret scoping:** per-secret IAM, not project-wide. Read vs send Azure
  secrets are accessible by different SAs (read → crown-sync, send → compute),
  so a compromise of one identity can't do the other's job.
- **Azure Mail scope:** both Graph apps are confined by Exchange
  `ApplicationAccessPolicy` (`RestrictAccess`) to the single
  `customerservice@lamppostglobes.com` mailbox — verified at the membership
  level for the read app's scope group and the send app's direct-mailbox scope.
- **PII:** the order mirror stores name/address/phone/email and the modeled
  Shift4 payload (`raw_payload`), but **no** payment/card data (clean PCI
  boundary). No read endpoint selects `raw_payload`. All reads are now admin-only
  (Finding 2).

## Consequences

- The one live exposure (Finding 1) is closed and verified; reads are admin-only
  (Finding 2). Deployed at v0.25.0.
- The Shift4 URL token is now scoped to its only legitimate purpose: authenticating
  the inbound webhook. It is not a credential for any read or dashboard.
- Security posture is documented and auditable for the consulting reference use.

## Future work

- `raw_payload` retains the full modeled Shift4 order indefinitely. For a client
  deployment, add a data-retention / right-to-deletion (GDPR/CCPA) policy.
- Consider enabling Security Command Center (standard tier is free) for ongoing
  misconfiguration scanning, and a periodic Cloud Audit Log review.
- Optional: move the PO routes from internal-guard to conditional registration
  for consistency with the read routes.

## References

- `webhook-handler/main.py` (`dashboard_html` registration), `webhook-handler/auth.py` (`is_authorized_read`)
- Builds on ADR-0013 (webhook URL token), ADR-0015 (conditional route registration), ADR-0021 (admin-only writes)
