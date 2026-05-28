# ADR-0008: Cloud SQL dev instance — cheapest viable configuration

**Status:** Accepted
**Date:** 2026-05-28

## Context

LPG-GCP needed a hosted Postgres 16 instance for dev work. The schema
had already been validated locally; the goal was to move from "schema
runs on my laptop" to "schema runs on GCP" without spending more on a
dev environment than necessary.

Cloud SQL has many configuration knobs that drive cost and capability:

- **Edition:** Enterprise vs. Enterprise Plus. Plus is HA-grade with
  better performance and starts at `db-perf-optimized-N-2` (~$200+/mo
  minimum). Enterprise allows shared-core tiers (~$10/mo minimum).
- **Tier (machine size):** `db-f1-micro` (cheapest, shared core, 0.6GB
  RAM), `db-g1-small` (shared core, 1.7GB), or `db-n1-standard-N`
  (dedicated cores, starting ~$50/mo).
- **Availability:** Zonal (single zone, default) or Regional (HA with
  failover, ~2x cost).
- **Storage:** 10GB minimum, auto-resize available.
- **Connectivity:** Public IP + Auth Proxy, or Private IP + VPC peering.
- **Backups:** On by default, ~7-day retention. Minor cost.

Because this is a learning project used intermittently, the priority
is "cheapest setup that's still legitimate and resizable later."

## Decision

| Knob | Choice |
|---|---|
| Edition | Enterprise |
| Tier | `db-f1-micro` (shared-core, 0.6GB RAM) |
| Availability | Zonal |
| Storage | 10GB SSD with auto-increase |
| Connectivity | Public IP, connect via Cloud SQL Auth Proxy |
| Backups | Default (enabled, 7-day retention) |
| Auth | Password on the `postgres` user, stored in 1Password |
| Activation policy | Toggled between `ALWAYS` (in-session) and `NEVER` (between sessions) for cost management |

Roughly ~$10/mo at 24/7 runtime, ~$1–3/mo if stopped between sessions.

## Consequences

**Positive:**

- Cheap enough for an intermittent dev project. Worst case (forgot to
  stop it for a month) is ~$15. Best case (stopped religiously) is
  ~$1–3.
- Every knob above is resizable later **without recreating the
  instance** — except Edition. The plan is "start cheap, pay for what
  you need when you need it."
- Auth Proxy avoids VPC setup, IP whitelisting, and certificate
  management. One less thing to learn before connecting from a laptop.
- Backups enabled by default means experimentation is safe — destructive
  mistakes recover from a 7-day point-in-time snapshot.

**Negative:**

- `db-f1-micro` shared-core has unpredictable performance. Other
  tenants on the same physical host can cause latency spikes. Fine for
  dev; would be unacceptable for production.
- Public IP, even when only reached via the Auth Proxy, has a non-zero
  attack surface. (Cloud SQL's network ACL defaults to "no IPs allowed"
  for direct connection, but the option to whitelist exists.) For
  production, switch to Private IP with VPC peering.
- Zonal means a single-zone outage downs the instance. Acceptable for
  dev; not for prod.
- Stop/start as a cost-management strategy depends on habit. Forgetting
  costs real (small) money. Mitigated by always ending a session with
  the stop command and verifying state at session start.

**Reversal cost:**

- **Edition cannot be changed in place.** Going Enterprise → Plus
  requires restoring a backup to a new instance. Plus → Enterprise is
  similarly invasive. So this is the one knob we should be deliberate
  about. For dev workloads, Enterprise is the right choice and we have
  no reason to flip.
- All other knobs are in-place editable, including tier resize (with a
  brief restart) and storage increase (online).

## Open questions

1. **IAM database authentication.** Cloud SQL supports IAM-based auth,
   which would eliminate password handling. Currently using password
   auth for setup simplicity. To revisit when we add additional users
   or roles — likely a follow-up ADR.
2. **Productionization path.** When LPG actually launches on GCP, a
   separate `lpg-prod` instance with Regional availability, Private
   IP, dedicated tier, and IAM auth will be needed. Plan it then, not
   now.

## Lessons captured during provisioning

- Cloud SQL defaulted to **Enterprise Plus** for the new instance,
  which rejects `db-f1-micro`. Explicit `--edition=ENTERPRISE` flag
  was required to get the cheap tier. Don't trust defaults silently.
- Instance creation took ~3 minutes — within the documented 5–15 min
  range but on the fast end.
- During the first session, the instance was accidentally stopped
  (activation policy flipped to `NEVER`) while the user stepped away.
  Cause was inadvertent — a closed terminal tab with a stale command.
  No GCP budget/billing automation was involved. Reinforced the habit
  of treating `gcloud sql instances patch` commands with the same care
  as destructive SQL.
