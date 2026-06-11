# ADR-0018: Purchase order generation

**Status:** Accepted **Date:** 2026-06-10 **Revised:** 2026-06-11 (implemented end-to-end and prod-verified: Q1 → separate send-only app; Q3 → full purchase_orders schema; build steps 1–5 live on v0.14.0, a real PO emailed via Graph Mail.Send and confirmed received. Q5 GCS storage deferred — PDFs render on demand. Earlier same-day: combo emits one PO line with joined SKU \+ summed cost \+ Shift4 description; Q4 PDF library resolved → reportlab). Prior revision 2026-06-10 (data verification; Q2 fees manual).

Accepted and implemented. The core generator, persistence, PDF rendering, generate/serve/send endpoints, and Graph send are built and verified in production (see **Implementation status** at the end). The 2026-06-10 revision folds in a working session that verified the explosion model against live `product_components` / `vendor_skus` / `shift4.order_items` data and closed the largest open question; see **Data verification & decisions** below.

## Context

The system can now ingest what Crown *actually charged* (crown-sync → `lpg.vendor_invoices`, ADR-0016/0017). Purchase-order generation is the upstream half of the same loop: producing the PO we send *to* Crown for a given customer order. Together they enable a three-way match — PO (what we ordered) → order confirmation (what Crown acknowledged) → invoice (what Crown billed).

This is not a greenfield feature. It has been built twice before:

1. **Salesforce** — `CrownPOGenerator` (Apex), `crownPoQuickAction` (LWC ScreenAction \= button \+ popup on the order record), `Crown_Purchase_Order` (Visualforce page rendered as PDF). Only the `-meta.xml` sidecars were available when drafting; the `.cls`/`.js`/`.page` source (which holds the exact fee logic and field mappings) should be pulled in at build time, the way ADR-0016 referenced the prior sync script.  
2. **Supabase/React CRM** — documented in `LampPostGlobes_CRM_BRD_v1.1.docx` (Phase 2, FR-01/FR-02). Per-order "Generate PDF" \+ "Email PO" buttons; PDF attached to the order; emailed to Crown.

So this ADR is primarily a **port to the GCP/all-Google \+ M365-tenant stack**, making the divergence calls where the new stack should improve on the old ones — not a from-scratch design.

A real example of the current PO output (PO32163) shows the target format:

- Header: PO Number, Date  
- Line items: Product ID | Item Description | Quantity | Cost (uses **LPG's** product IDs, e.g. `88267-cl-5n`, not Crown's)  
- Fee as a sparse line item: `Order Fee 15.00` (no product ID/qty)  
- Ship-to: the **end customer** (dropship model — ship-to pulled from the Shift4 order)

## Decisions made

1. **Pack quantities: order exact need, eat the fee.** PO-gen does NOT round up to Crown's pack quantities (64/27/8/8) to avoid the broken-carton fee. It orders the real need; if that incurs a fee, so be it. (See open question on where pack quantities are stored — needed to *predict* the fee, not to avoid it.)  
     
2. **Fees are itemized as PO line items.** The PO must list applicable fees (min-order, broken-carton) as explicit lines, mirroring Crown's own invoice/confirmation structure. This means PO-gen **computes fees at generation time** from the `lpg.vendors` columns (`min_order_fee`, `broken_carton_fee`, `min_order_threshold` — already seeded $15/$15/$100). The PO becomes an honest forecast of Crown's total bill, strengthening the three-way match.  
     
3. **Manual, per-order generation.** No demand forecasting. The user opens an order and generates its PO on command (matches BRD FR-01: a button on the Order Detail page).  
     
4. **Output: generate PDF, human reviews, then chooses to send.** A review step sits between generation and sending.  
     
5. **Run surface: request/response on the `lpg-admin` service, NOT a Cloud Run job.** PO-gen is interactive (manual trigger \+ human-in-loop review), unlike the batch crown-sync job. It fits `lpg-admin` (IAM-protected, already hosts read endpoints). Proposed endpoints:  
     
   - `POST /purchase-orders` (or `/orders/{id}/purchase-order`) — generate a draft PO \+ PDF for an order  
   - `POST /purchase-orders/{id}/send` — email it to Crown after review No new image/surface; reuses the existing auth boundary.

   

6. **Send mechanism: Microsoft Graph `Mail.Send` from the M365 tenant — NOT browser automation.** The prior CRM (BRD FR-02) sent via Claude-in-Chrome browser automation against personal `lamppostglobes@outlook.com`. The GCP architecture deliberately moved off the personal account \+ browser hacks onto the corporate tenant \+ Graph service principal (ADR-0016/0017). PO send should follow suit: API-native, durable, transferable. The composer "popup" is a frontend modal in the admin UI (review/edit body, PO attached); the actual send is a server-side `lpg-admin` endpoint calling Graph `Mail.Send`.  
     
7. **Explosion model: selective, driven by `product_components`; passthrough is the absence of a row.** *(Resolves the explosion half of the original Q2/Q3 — see Data verification below.)* Unlike the Salesforce version, GCP order line items arrive **un-exploded**: combo SKUs land in `shift4.order_items` as single lines, because GCP has no upstream order-import step that splits them (Salesforce did). PO-gen therefore performs the decomposition itself, at generation time, per order line:  
     
   - SKU **has rows** in `lpg.product_components` → **one** PO line whose Product ID is the component codes joined with `/` (e.g. `20012-WH-XX/98006-P`, Crown's "converted id" convention) and whose unit cost is the **summed** component cost from `lpg.vendor_skus`. Only the SKU *value* is exploded — the line stays a single line. Quantity is the order quantity; the line's description is Shift4's own order-item description, verbatim.  
   - SKU **has no rows** → passthrough: the LPG SKU *is* the Crown SKU; emit it verbatim (e.g. `20012-CL-4F → 20012-CL-4F`), priced from its own `vendor_skus` row, with Shift4's order-item description.

   

   **Invariant:** a SKU appears in `product_components` *if and only if* it decomposes into *different* components. This keeps the BOM table a pure exception list (24 combos / 48 components) — self-documenting, with zero maintenance for the hundreds of passthrough SKUs that will never combo.

   

8. **Pricing: `lpg.vendor_skus.unit_cost` is the single source of truth.** Combo lines sum their component costs; passthrough lines read their own row. The combo-level `cost` column in the source `custom_products.csv` is a discarded cached field — it was also blank for the 20010/20012 families, which price correctly from components regardless.

## Findings from the prior Apex source (resolves several open questions)

Reading the Salesforce source (`CrownPOGenerator.cls`, `CrownPOController.cls`, `CrownPOGeneratorTest.cls`) shows the proven implementation is **simpler** than first assumed. Key facts:

- **No fee calculation logic exists.** The broken-carton and minimum-order fees are NOT computed from rules. They are stored as fields on the Order (`Broken_Carton_Fee__c`, `Minimum_Order_Fee__c`) and the generator simply *prints whatever value is there*. The test fixture sets both to 15 as input data. → **Resolves Q2:** GCP does not need pack-quantity schema or threshold logic to ship a faithful port. Fees are values carried on the PO (set manually, or by a simple rule we may add *later* as an enhancement, not a prerequisite).  
- **No kit explosion, no SKU mapping.** `CrownPOController` reads the order's `OrderItem`s and prints `Product2.ProductCode` directly — LPG's own SKU (matches the PO32163 sample showing `88267-cl-5n`). Crown maps these on their end. → **Reframes the explosion question (now Decision 7):** the proven behavior is a straight passthrough of the order's line items in LPG SKUs. Kit explosion / vendor-SKU mapping was never part of PO-gen. Adding it in GCP is an *optional enhancement*, an explicit choice — not required to match current behavior. **\[Superseded by Data verification 2026-06-10:** this held in Salesforce *because the combo→component split happened upstream at order import*, so the order's line items were already exploded by PO time. GCP has no such upstream step — combos arrive un-exploded — so explosion **is** required here. It is selective (only combos in `product_components`); passthrough remains the default. See Decision 7 and Data verification below.**\]**  
- **PO number \= Shift4Shop order number** (fallback to the internal order number). → **Resolves Q4 numbering:** no sequence to mint; reuse the storefront order number. Per **ADR-0009**, this human identifier is `shift4.orders.invoice_number` (= `InvoiceNumberPrefix + InvoiceNumber`, e.g. `PO32163`) — the value 0009 calls out as *"the value that appears on Crown invoices,"* i.e. the literal three-way-match join key. So `purchase_orders.po_number` sources from `shift4.orders.invoice_number`, **not** the internal numeric `shift4_order_id` (which is retained as the FK for joins).  
- **Generation \= render PDF \+ attach to the order.** `CrownPOGenerator` renders the Visualforce page to PDF and saves it as a File linked to the order. GCP equivalent: render PDF → store in GCS → link to the order row.

### PDF data contract (from CrownPOController)

The PDF template consumes exactly: `poNumber`, `today`, `commentsText`, `brokenCartonFee`, `minimumOrderFee`, ship block (`shipName`, `shipCompany`, `shipStreet`, `shipCityLine`, `shipPhone`), and line items of (`productCode`, `description`, `quantity`, `unitPrice`). That is the full field set the GCP PDF builder must supply. Under Decision 7, a combo order produces **one** line: `productCode` is the joined component SKUs (e.g. `20012-WH-XX/98006-P`), `unitPrice` is the summed component cost, and `description` is Shift4's order-item description; passthrough lines carry the SKU itself, also with the Shift4 description. (The Shift4 description replaces `vendor_skus.description`, which per ADR-0014 holds the price-list *category*, not a product label.)

## Data verification & decisions (2026-06-10)

A working session verified the explosion model against live data, resolving the largest open question (the explosion half of Q2/Q3). Findings, in order:

- **GCP stores combos un-exploded.** `shift4.order_items` holds combo SKUs as single lines (confirmed: `20012-CL-4F`, `20014-WH-6F`). In Salesforce the combo→component split happened *upstream*, at order import, which is why its PO-gen could passthrough. GCP has no such step, so **PO-gen must explode** — but selectively (only combos in the list).  
- **`custom_products.csv` is the explosion source** — 24 combos, each decomposing to a globe \+ a neck (e.g. `20012-WH-6F → 20012-WH-XX / 98006-P`). It is an **exception list, not a full catalog**: SKUs absent from it pass through unchanged (the LPG SKU *is* the Crown SKU).  
- **The mapping is already seeded** in `lpg.product_components` (24 kits / 48 components), referencing components by `vendor_sku_id` (FK into `vendor_skus`).  
- **Seed cleanup performed.** Two self-referential rows — `20012-CL-4F → 20012-CL-4F` and `20014-WH-6F → 20014-WH-6F`, both tagged "direct passthrough" in their `notes` — were **deleted**. They were the half-started seed of an alternative "explicit 1:1" model (Design B), in which *every* SKU gets a `product_components` row. That model was rejected in favor of "passthrough \= absence of a row" (Design A): a BOM table whose every row is a genuine decomposition, with no obligation to seed the hundreds of non-combo SKUs. Post-cleanup count confirmed: **24 kits / 48 components.**  
- **Pricing verified.** Both passthrough order SKUs exist in `vendor_skus` with real costs (`20012-CL-4F` $12.40, `20014-WH-6F` $29.00, both active); all 24 combos sum to component-level costs from `vendor_skus`, including the four families that were cost-blank in the CSV. This confirms Decision 8: `vendor_skus` prices everything; the CSV combo-cost is discarded.

**Net effect on open questions:** the explosion/SKU-mapping question is now **closed** (Decision 7). What remains genuinely open is fee handling (Q2, narrowed below), PO persistence schema (Q3), the PDF library (Q4), GCS storage (Q5), and the `Mail.Send` app boundary (Q1).

**Watch notes (not blockers, but track these):**

- **Fee-line symmetry with `vendor_invoice_lines`.** PO fee lines carry the fee in a dedicated `amount` column (product fields null). Migration 0003 added `is_fee` to `vendor_invoice_lines` (the invoice side of the three-way match). Confirm the invoice side represents fee amounts compatibly, or PO↔invoice fee matching won't line up cleanly. (Columns of `vendor_invoice_lines` not re-verified here.)  
- **New-combo passthrough gap (ties ADR-0010).** ADR-0010 auto-stubs unknown SKUs into `shift4.products` at order ingest. A *new* combo not yet in `product_components` would stub in and then **silently pass through** to Crown as a single combo SKU Crown can't fulfill — the data-quality failure mode of the invariant, now with a concrete entry path. The mitigation is keeping `product_components` authoritative; worth a guard or report that flags order SKUs which look like combos but have no BOM rows.  
- **`lpg-admin` gains mutating endpoints.** ADR-0015 framed `lpg-admin` as read-only; PO-gen adds `POST` generate/send. No conflict (the compute SA holds full DML on `lpg.*` per ADR-0012, and default privileges cover the new tables) — just acknowledged here so the read-only framing isn't taken as still-current.

## Open questions (resolve before build)

### Q1 — `Mail.Send`: extend the existing Azure app, or a separate app? — **RESOLVED (separate send-only app)**

**Decision: a separate send-only Azure app (option b).** Adding `Mail.Send` to the read app would undo the least-privilege boundary ADR-0017 established — one credential able to both read and send mail is precisely what a client security review rejects. Instead, a distinct app ("Lamp Post Globes — Crown PO Send", client `3e9eda8a-…`) holds `Mail.Send` Application permission *only*; the read app keeps `Mail.Read` *only*. Both share the tenant (`fa215d01-…`).

**As built and verified in production:**
- New app registration, single-tenant, no redirect URI; `Mail.Send` Application permission with admin consent (no `Mail.Read`).
- `New-ApplicationAccessPolicy -AccessRight RestrictAccess` scoping the app to the single mailbox `customerservice@lamppostglobes.com` (direct-to-mailbox scope, no scope group — simpler than ADR-0017's group, equally correct for one mailbox). Same multi-hour propagation as ADR-0017; `Test-ApplicationAccessPolicy` again reports `Granted` instantly and is not trusted.
- Client secret stored in Secret Manager as `azure-graph-send-secret` (Value, not Secret ID — verified 40 chars per the ADR-0017 trap), readable by the `lpg-admin` compute SA via a secret-scoped `secretAccessor` binding.
- `lpg-admin` env: `AZURE_TENANT_ID`, `AZURE_SEND_CLIENT_ID`, `CROWN_PO_MAILBOX` (plain) + `AZURE_SEND_CLIENT_SECRET` (secretKeyRef). Send client `graph_mail.py` mirrors crown-sync's MSAL client-credentials pattern with the send app's own creds.
- Send endpoint `POST /purchase-orders/{po_number}/send` (lpg-admin only): loads the stored PO, renders the PDF, sends via Graph, marks `status=sent` + `sent_at`. **Never auto-sends** — explicit call only. Double-send guard returns `409` unless `?force=true`; `422` if the vendor has no `po_email`; `502` (and PO stays `draft`) if Graph fails. Recipient sourced from `lpg.vendors.po_email`.

A real PO (`PO32159`) was generated, rendered, and emailed end-to-end through the live service, landing in a real inbox with the PDF attached; the `409` double-send guard was confirmed live.

### Q2 — Fee handling: computed vs. manually-set values — **RESOLVED (manual)**

*(The explosion half of the original Q2 was resolved by Decision 7\. The fee half is now resolved here.)*

**Decision: carry fees as manually-set values for now; computed fees are a deferred enhancement.** The Salesforce version did **not** compute fees — `Broken_Carton_Fee__c` / `Minimum_Order_Fee__c` were values set on the Order and printed as-is — and GCP follows that proven path first. PO fees are entered/explicit and itemized as `is_fee` lines; PO-gen does **not** read the `lpg.vendors` thresholds (`min_order_fee` / `broken_carton_fee` / `min_order_threshold`, still seeded $15 / $15 / $100) at generation time, and **no `pack_quantity` on `vendor_skus` is added** — it was only needed to *detect* broken-carton cases for computed fees. Computed fees (reading the vendor thresholds \+ pack quantities to make the PO a true forecast of Crown's bill for the three-way match) remain a clean follow-up, to be added deliberately when wanted. With this, all prerequisite questions for the core generator are closed; what remains (Q3–Q5, Q1) is persistence/output/send plumbing.

### Q3 — Schema: how much to persist? — **RESOLVED (full schema)**

**Decision: option (b) — a full `purchase_orders` + `purchase_order_lines` schema.** The three-way-match goal (PO ↔ confirmation ↔ invoice) is core, so POs are first-class rows, not just a PDF pointer + fee fields on the order. Migration 0004 added both tables (`is_fee` on lines, fee amount in a dedicated `amount` column, bigserial PKs, `set_updated_at` triggers, a `chk_purchase_order_lines_kind` CHECK enforcing fee-vs-product line shapes); migration 0005 added the self-referential guard enforcing Decision 7's explosion invariant. `po_number` sources from `shift4.orders.invoice_number` (ADR-0009 join key). Write is idempotent on `po_number` (regeneration replaces lines and resets to `draft`). The generated PDF is *not* persisted — it renders on demand from the stored rows (`load_purchase_order` → `render_purchase_order_pdf`), which is why Q5 (GCS) is deferred rather than required.

### Q4 — PDF generation library — **RESOLVED (reportlab)**

**Decision: reportlab (Platypus).** The fork was HTML-template→PDF (WeasyPrint, closest to the Visualforce model) vs. programmatic (reportlab). reportlab wins on the deciding factor for an all-Cloud-Run stack: it is **pure-Python with no native system dependencies**, so the image needs no apt packages (WeasyPrint pulls pango/cairo/gdk-pixbuf — image bloat a reviewer questions). A PO is a standard tabular document (header, ship-to, line-items grid, fees, total) that Platypus `Table` handles cleanly, and the Visualforce layout was never provided, so there is no pixel-target that would have favored HTML/CSS. Implemented in `purchase_order_pdf.py` as a pure `render_purchase_order_pdf(po) -> bytes` (no DB, no I/O target); `reportlab` added to `webhook-handler/requirements.txt`. Verified rendering the PO32163 field set end-to-end.

### Q5 — PDF storage / order linkage

Store generated PDFs in a GCS bucket (`purchase-orders/`) referenced from the order/PO row. Confirm bucket \+ linkage approach.

## Supersedes / relationship to the BRD

The BRD's FR-07 ("Invoiced Cost" manual field) and FR-08 ("Find Invoice" via browser-based PDF reading) are **already obsolete** in GCP — crown-sync automates structured invoice ingest into `vendor_invoices`, surpassing the manual single-field approach. PO-gen closes the loop on the *outbound* side. The three-way match (PO ↔ confirmation ↔ invoice) is the GCP system's superset of the BRD's order-cost tracking.

## Amends ADR-0004 (and ADR-0003's COGS model)

Decision 7's explosion invariant **reverses a load-bearing rule in ADR-0004.** This must be recorded explicitly, because changing it silently would leave the decision corpus self-contradictory.

**What ADR-0004 mandated.** ADR-0004 ("Represent kits via a BOM table") states that *every sellable product has at least one `product_components` row* — "a 'simple' product is just a product with one BOM row at `quantity = 1`," and explicitly accepts the cost that "a non-kit product still needs a BOM row." Under that rule, the two self-referential rows we deleted (`20012-CL-4F → 20012-CL-4F`, `20014-WH-6F → 20014-WH-6F`, qty 1\) were **correct** — and their `notes` ("direct passthrough — storefront SKU \= vendor SKU") show they were written deliberately to honor 0004\. They were not stray seed artifacts; they were 0004 being obeyed.

**What Decision 7 changes it to.** A product has `product_components` rows **iff** it decomposes into *different* components. No row \= passthrough. The "one BOM row per simple product" requirement is withdrawn. `product_components` becomes a pure exception list (24 combos / 48 components), not a universal catalog.

**Why the change.** The universal-row model obligates seeding a BOM row for every one of LPG's hundreds of passthrough SKUs, forever, with no information gain — every such row would just point at itself. The exception-list model is self-documenting (every row is a real decomposition) and zero-maintenance. The live data was already non-compliant with 0004 (only combos were ever seeded), so this aligns the rule with reality rather than diverging from it.

**Consequence for ADR-0003 (COGS) — must be honored when COGS is built.** ADR-0003's costing model is "join product → `product_components` → `vendor_skus`, sum." Under the exception-list invariant, that join returns **nothing** for a passthrough product (it has no BOM rows), which would silently under-cost or drop every passthrough product from any margin report. COGS must therefore use the **same explode-or-passthrough fallback PO-gen uses:** if the product has BOM rows, sum them; if not, read the product's own `vendor_skus` row. This is a pleasing symmetry — COGS and PO-gen share one rule — but it is currently **undocumented in 0003** and must be implemented when the COGS view (`lpg.product_cogs_v`, if/when built) is created. If that view already exists, it needs auditing for this exact bug before it is trusted.

**0004 otherwise stands.** The separate-BOM-table choice, the `(product_sku, vendor_sku_id)` uniqueness, the COGS-via-sum approach, the packing-list use — all unchanged. Only the "every product needs a row" population rule is superseded.

## Build order (status as of 2026-06-11)

1. ✅ **Done.** Schema: `purchase_orders` \+ `purchase_order_lines` (with `is_fee`). No `pack_quantity` on `vendor_skus` (Q2 resolved manual — not needed); explosion uses the already-seeded `product_components`.  
2. ✅ **Done (prod-verified).** Core logic — a pure, testable module (like the parser/writer split): order line → `product_components` lookup → **one line per order line** (combo \= joined component SKU \+ summed cost; else passthrough) → price from `vendor_skus` → append fee line(s). Description \= Shift4 order-item text. **Built:** `purchase_order_builder.py` (pure) \+ `purchase_order_repository.py` (fetch/persist) \+ `smoke_po.py`, verified live.  
3. ✅ **Done (prod-verified).** PDF builder reproducing the PO32163 layout (Q5).  
4. ✅ **Done (prod-verified).** `lpg-admin` endpoints: generate (returns draft \+ PDF), send.  
5. ✅ **Done (prod-verified — real send to inbox).** Azure `Mail.Send` setup per Q1; send endpoint via Graph.  
6. ⬜ Admin-UI composer modal (review/edit/send).  
7. ⬜ GCS storage \+ order linkage (Q5).  
8. ⬜ Terraform the new infra (send app/permissions, GCS bucket) — first real *use* of the Terraform foundation, which is committed (`233e289`: GCS backend, pinned provider, variabilized project/region) but manages **zero resources** to date. PO-gen's resources are written in Terraform from birth, per the deliberate "defer importing existing infra" strategy set when the foundation landed. (The foundation itself is not yet recorded in an ADR — see References.)

## Implementation status (2026-06-11)

Built, committed, and verified in production on `v0.14.0`:

- **Schema** — migrations 0004 (PO tables) + 0005 (explosion-invariant guard), applied live.
- **Builder / repository / PDF / smoke CLI** — proven against a real-Postgres sandbox and the live DB.
- **Endpoints on `lpg-admin`** (admin-only, 404 on the public webhook-handler):
  - `POST /orders/{shift4_order_id}/purchase-order` — generate/regenerate a draft PO.
  - `GET /purchase-orders/{po_number}/pdf` — render a stored PO to PDF on demand.
  - `POST /purchase-orders/{po_number}/send` — email the PO to the vendor, mark sent. Never auto-sends; `409` double-send guard (`?force=true` to resend).
- **Graph `Mail.Send`** — separate send-only app, mailbox-scoped Application Access Policy, secret in Secret Manager, creds on `lpg-admin`. A real PO was emailed end-to-end and received with the PDF attached.

**Workflow (confirmed):** generation is **manual** (per-order, on command) and send is **manual** (explicit action; a composer popup will replace the direct call). Nothing is ever auto-generated or auto-sent to the vendor.

**Still open:** build steps 6–8 — the composer modal, GCS storage (Q5), and Terraform of the new infra — tracked in `BACKLOG.md`.

## References

- BRD: `LampPostGlobes_CRM_BRD_v1.1.docx` (Phase 2, FR-01/FR-02)  
- Prior implementation (Salesforce): `CrownPOGenerator` (Apex), `crownPoQuickAction` (LWC), `Crown_Purchase_Order` (Visualforce) — source to be pulled at build time  
- Current PO output sample: PO32163  
- Related ADRs: **0003** (vendor cost on `vendor_skus`; COGS model — see Amends), **0004** (BOM table — amended above), **0009** (webhook contract; `invoice_number` \= the PO number), **0010** (product stub auto-create — new-combo watch note), **0012** (IAM DB auth; `lpg.*` DML grants cover the new tables), **0015** (webhook/admin split — `lpg-admin` gains mutating endpoints), **0016** (invoice ingest), **0017** (scope lockdown \+ restructure; `Mail.Send` precedent for Q1), and the three-layer product architecture (`vendor_skus` / `product_components`)  
- **Terraform foundation** (`233e289`) is committed but **not yet recorded in an ADR.** It manages zero resources; PO-gen is its first use. Worth a short ADR-0019 documenting the foundation and its "defer importing existing infra / write new resources from birth" strategy — referenced by build-order step 8\.

