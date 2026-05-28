# ADR-0001: Split storefront and back-office data into two schemas

**Status:** Accepted
**Date:** 2026-05-27

## Context

LPG has two distinct kinds of data:

1. Data that originates in Shift4Shop (the storefront) — customers,
   orders, line items, catalog. Shift4 is the system of record.
2. Data that only LPG knows about — vendors, vendor SKUs, purchase
   orders, invoices, kit compositions, return authorizations. Shift4
   has no concept of any of these.

If we put all of this in one undifferentiated `public` schema, two
problems show up fast:

- It becomes ambiguous who is allowed to write what. Webhook handlers
  start updating tables they shouldn't. Internal apps start mutating
  rows that should be Shift4-authoritative.
- When we later need to grant a service account write access to "the
  webhook tables" but not "the internal tables," we have no clean
  boundary to grant on.

## Decision

Use two Postgres schemas with explicit ownership:

- **`shift4`** — mirror of data ingested from Shift4Shop. Written only
  by the webhook ingest path. Treated as read-mostly by everything else.
- **`lpg`** — back-office data owned by LampPostGlobes. Written by
  internal apps and manual entry.

The schema name encodes the source-of-truth.

## Consequences

**Positive:**

- Source-of-truth ownership is visible in every query (`SELECT ... FROM
  shift4.orders` vs `SELECT ... FROM lpg.vendors`).
- Grants are clean: webhook service account gets `INSERT/UPDATE` on
  `shift4.*` only. Back-office app gets full access to `lpg.*` and
  `SELECT` on `shift4.*`.
- If we add another sales channel later (Amazon, eBay, wholesale), it
  gets its own schema — `amazon.*`, `wholesale.*` — without disrupting
  anything that already exists.
- Joining across schemas is normal SQL. There's no penalty.

**Negative:**

- Two-namespace mental overhead. Every table reference needs the schema
  prefix (we don't use `search_path` tricks because they obscure intent).
- A few utility functions (`lpg.set_updated_at()`) end up triggered from
  the "wrong" schema's tables. Acceptable — the function is an LPG-owned
  utility that happens to be applied to `shift4.*` tables too.
