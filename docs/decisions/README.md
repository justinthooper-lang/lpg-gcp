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
| [0009](./0009-shift4-webhook-contract.md) | Shift4 webhook contract and resulting schema additions | Accepted (partially superseded) |
| [0010](./0010-product-stub-auto-create.md) | Auto-create product stubs from order ingest | Accepted |
| [0011](./0011-cloud-run-deploy-architecture.md) | Webhook handler deploy architecture on Cloud Run | Accepted |
| [0012](./0012-iam-database-auth.md) | IAM database authentication on Cloud Run | Accepted |
| [0013](./0013-url-token-auth-for-shift4.md) | URL token authentication for Shift4 webhooks | Accepted |
| [0014](./0014-vendor-pricing-snapshot-pattern.md) | Vendor pricing snapshots — PDF, CSV, seed script | Accepted |
| [0015](./0015-split-webhook-and-admin-services.md) | Split webhook-handler and lpg-admin into separate Cloud Run services | Accepted |
| [0016](./0016-vendor-invoice-ingest-from-outlook.md) | Ingest Crown invoices via Cloud Run job + Microsoft Graph service principal | Accepted |
| [0017](./0017-crown-sync-hardening-and-restructure.md) | Crown-sync hardening — mailbox scope lockdown, forward-resilient filtering, shared package | Accepted |
| [0018](./0018-purchase-order-generation.md) | Purchase order generation | Accepted |
| [0019](./0019-terraform-foundation-and-import-deferral.md) | Terraform foundation and the import-deferral strategy | Accepted |
| [0020](./0020-cloud-run-script-managed.md) | Cloud Run stays script-managed; deploy scripts own the full service shape | Accepted |

## How to add a new ADR

1. Pick the next number. Use a short slug. File name: `NNNN-short-slug.md`.
2. Copy the structure from any existing ADR.
3. Set status to `Accepted` (or `Proposed` if you're floating the idea).
4. Add the row to the index above.
5. Commit. ADRs are merged with the change they describe, not separately.
