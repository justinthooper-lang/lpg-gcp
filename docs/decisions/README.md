# Architecture Decision Records

Each file in this folder records one architectural decision: what we
decided, why, and what the trade-offs are. They are **append-only**.
If a decision changes, write a new ADR that supersedes the old one;
don't edit history.

The format is loosely [Michael Nygard's
template](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions):
Context → Decision → Consequences. Short is fine. The point is that
future-you (or future-me) can read these in 5 minutes and understand why
the system looks the way it does.

## Index

| # | Title | Status |
|---|---|---|
| [0001](./0001-shift4-lpg-schema-split.md) | Split storefront and back-office data into two schemas | Accepted |
| [0002](./0002-address-denormalization.md) | Denormalize addresses on orders and shipments | Accepted |
| [0003](./0003-vendor-cost-in-vendor-skus.md) | Vendor cost lives on `lpg.vendor_skus`, not on products | Accepted |
| [0004](./0004-bom-via-product-components.md) | Represent kits via a bill-of-materials table | Accepted |
| [0005](./0005-order-items-fk-to-products.md) | Webhook ingestion FK race conditions | Accepted with caveats |
| [0006](./0006-public-repo.md) | Keep the repo public | Accepted |
| [0007](./0007-collapse-account-contact-into-customers.md) | Collapse Account + Contact into a single `shift4.customers` table | Accepted |
| [0008](./0008-cloud-sql-provisioning.md) | Cloud SQL dev instance — cheapest viable configuration | Accepted |
| [0009](./0009-shift4-webhook-contract.md) | Shift4 webhook contract and resulting schema additions | Accepted |
| [0010](./0010-product-stub-auto-create.md) | Auto-create product stubs from order ingest | Accepted |
| [0011](./0011-cloud-run-deploy-architecture.md) | Webhook handler deploy architecture on Cloud Run | Accepted |
| [0012](./0012-iam-database-auth.md) | IAM database authentication on Cloud Run | Accepted |

## How to add a new ADR

1. Pick the next number. Use a short slug. File name: `NNNN-short-slug.md`.
2. Copy the structure from any existing ADR.
3. Set status to `Accepted` (or `Proposed` if you're floating the idea).
4. Add the row to the index above.
5. Commit. ADRs are merged with the change they describe, not separately.
