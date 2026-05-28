# ADR-0007: Collapse Account + Contact into a single `shift4.customers` table

**Status:** Accepted
**Date:** 2026-05-28

## Context

LPG's existing Salesforce CRM uses the standard SF pattern of one
**Account** record plus one **Contact** record per Shift4 customer.
Person-level data (first/last name, email, phone, mailing address)
lives on the Contact. The Account holds whatever the customer typed
at checkout (which may be a real company name or just the person's
name) plus the `Shift4Shop_Customer_ID__c` external ID.

We considered three approaches for mirroring this structure in Postgres:

1. **Mirror the SF structure exactly** — separate `shift4.companies`
   and `shift4.customers` tables linked by FK.
2. **Collapse into one table** — single `shift4.customers` table
   containing both account-level and contact-level data.
3. **Hybrid (Shopify pattern)** — single `shift4.customers` table with
   an optional `company_id` foreign key to a `shift4.companies` table
   for the genuine B2B subset.

Real LPG data was used to evaluate. Two reports were pulled:

- 17 recent Accounts showed mixed Account names — some real companies
  (`Tait Towers`, `Mc3lroy Architecture`, `Warner Bros Pics`) and some
  bare person names (`Patricia Dougherty`, `Rohn Ramon`,
  `Louise de Kluyver`). The `Account_Type__c` picklist that should
  distinguish B2B from B2C is **not populated** on any of them.
- The matching Contact records confirmed that every Account had at
  least one Contact, and that for the bare-person-name Accounts, the
  Contact name was identical to the Account name — pure redundancy.

So in the actual data:

- For B2C customers, the Account/Contact split adds zero information.
- For B2B customers, the split carries real information (company vs.
  buyer), but there's no reliable way to tell B2C from B2B at ingest.
- The integration creates duplicate Accounts on every guest checkout
  (the same `Seven Fields` company appears as 3 separate Account
  records with 3 different Shift4 customer IDs), so even the "real
  company" Accounts aren't a clean source of truth for companies.

## Decision

Use **option 2** — one `shift4.customers` table. No `shift4.companies`
table.

Column mapping from the SF model:

- SF `Contact.FirstName`, `LastName`, `Email`, `Phone`, `MobilePhone`
  → `shift4.customers.first_name`, `last_name`, `email`, `phone`
- SF `Account.Name` → `shift4.customers.company_name`
  (kept as `company_name` even though it may contain a person's name —
  the column reflects what Shift4 captured at checkout)
- SF `Account.Shift4Shop_Customer_ID__c` →
  `shift4.customers.shift4_customer_id` (PK)
- SF Contact mailing address → **not mirrored on customers**; addresses
  are stored on `shift4.orders` and `shift4.shipments` instead, per
  [ADR-0002](./0002-address-denormalization.md)

## Consequences

**Positive:**

- The schema models what the data actually looks like, not what we'd
  like it to look like.
- Avoids a `shift4.companies` table that would be ~50% junk rows
  (duplicated person names dressed up as company records).
- Simpler queries. "Get this customer's orders" is one JOIN, not two.
- Matches what most B2C-first ecommerce platforms (Shopify, WooCommerce)
  do natively, so common patterns and tooling work without contortion.

**Negative:**

- If LPG grows into a real B2B sales motion — multiple buyers per
  company, company-level pricing, shared invoicing — we will need to
  introduce a companies table. That will be a new ADR superseding this
  one. The migration path: add `shift4.companies`, add nullable
  `company_id` FK on `shift4.customers`, backfill by clustering
  customers with matching `company_name`. Real but not enormous.
- Some B2B-flavored facts are currently hard to express. "Brian Hitch
  and Joshua Loew both buy on behalf of Tait Towers" appears in the
  schema as two customer rows with the same `company_name` — not as
  one company with two associated people. Acceptable today because
  this isn't a workflow LPG actively uses.
- Guest checkout duplication remains an unsolved problem (same person
  buying as guest 3 times = 3 customer rows). This isn't an artifact
  of the collapse decision — it would happen in any of the three
  options — but it's worth naming here so it's not blamed on this ADR
  later. See [the architecture doc's "Known issues"
  section](../architecture.md#known-issues--open-questions).

**Trade-off explicitly accepted:** we're optimizing for the shape of
the data LPG actually has today, not for hypothetical B2B sophistication
we don't currently use. When the business needs that sophistication, we
add the companies table and supersede this ADR. Until then, simpler is
better.
