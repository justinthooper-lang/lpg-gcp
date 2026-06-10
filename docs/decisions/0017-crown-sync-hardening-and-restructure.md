# ADR-0017: Crown sync hardening — mailbox scope lockdown, forward-resilient filtering, and shared-package restructure

**Status:** Accepted **Date:** 2026-06-09

## Context

ADR-0016 designed the Crown invoice ingest and was accepted before the code was written. Implementation revealed several places where the real system had to diverge from that design, and one security item queued in 0016 ("future work") came due. This ADR records the security decision in full and amends ADR-0016 where reality differed, so the decision set stays honest about how the system actually works.

The driving security concern: the Azure app registration ("Lamp Post Globes — Crown Invoice Sync", client ID c36883bf-...) held Mail.Read Application permission scoped to the **entire** lamppostglobes.com tenant. ADR-0016 noted the practical blast radius was small (the tenant effectively has one mailbox) and deferred the lockdown. But the project's purpose is a reference architecture a client would trust, and a tenant-wide mail-reading service principal is exactly what a client security review rejects. "Only one mailbox exists today" is the rationalization that ships over-permissioned apps. So this got closed now, before more was built on top of it.

## Decision

### 1. Restrict Mail.Read to a single mailbox via Application Access Policy

The Azure app is now scoped, in Exchange Online, to read only customerservice@lamppostglobes.com — not the tenant.

Mechanism (Exchange Online PowerShell):
- A mail-enabled security group, crown-sync-scope@lamppostglobes.com, whose only member is customerservice@lamppostglobes.com.
- New-ApplicationAccessPolicy -AppId c36883bf-... -PolicyScopeGroupId crown-sync-scope@lamppostglobes.com -AccessRight RestrictAccess.

RestrictAccess is deny-by-default: the app can touch only mailboxes in the group. To grant access to another mailbox later, add it to the group — never broaden the app's permission. (Salesforce analogy: the gris a public group, the policy is a sharing rule binding the integration user to it.)

Verified with Test-ApplicationAccessPolicy: customerservice@ returns Granted. A denied-mailbox negative test isn't possible today — the tenant has only the one mailbox — but the policy will apply automatically to any mailbox added later.

### 2. Filter Crown mail on subject + attachment, never on sender

ADR-0016 step 3 filtered on from == crown@plasticglobes.com. **That is replaced.** Crown's mail reaches the tenant by being *forwarded*, and a forward rewrites the From header to the forwarding mailbox. Sender-based filtering matches zero forwarded invoices.

The durable signal is what survives a forward: the subject signature (Invoice/Tracking Information- <num>-Crown Plastics) plus an invoice_*.pdf attachment. The filter now keys on those.

### 3. Dedup on invoice number, not message ID

ADR-0016 made graph_message_id the idempotency key. Implementation revealed Crown sends **two identical emails per invoice** (a venside quirk they cannot fix). The two copies have different message IDs, so a message-ID key would insert both. The real business identity is the invoice number, so the writer dedups on the existing uq_vendor_invoice_number (vendor_id, vendor_invoice_number) constraint (ON CONFLICT ... DO NOTHING). This skips both Crown's second copy and any genuine re-sync. First copy to arrive wins; since the copies are identical, which one is irrelevant. graph_message_id is still stored for provenance.

### 4. No Outlook category-tagging; DB constraint is the sole idempotency guard

ADR-0016's "Negative consequences" flagged that tagging needs Mail.ReadWrite (broader than wanted) and predicted we'd likely drop it. We did. Idempotency is the DB unique constraint alone (point 3). The app keeps Mail.Read only — consistent with the scope lockdown's least-privilege intent.

### 5. Shared code extracted into an installable lpg_common package

ADR-0016 assumed the job would import webhook-handler/db.py directly (the script waso be a port of reference/crown_invoice_sync.py). Implementation instead produced three purpose-built modules (crown_invoice_parser, crown_invoice_writer, sync_crown_invoices) and exposed a packaging problem: the deployable code and db.py live in webhook-handler/ (the Docker build context), but the Crown code lives in scripts/ at repo root — outside that context. It cannot ship in the webhook-handler image, and the webhook service's entrypoint is uvicorn, wrong for a batch job.

Resolution: db.py moved into a new installable package lpg_common/ (with pyproject.toml, owning the DB-connection dependency stack). Both the webhook service and the Crown job depend on lpg_common via pip install, eliminating sys.path hacks. This sets up the planned **two-image split** (see Future work): a public webhook-service image with no mail-reading code, and a separate Crown-job image with no web server — a clean least-privilege boundary. The package is the seam that split happens along.

## Operational note: policy propagn delay

RestrictAccess enforcement does **not** apply instantly. After creating the policy, Graph calls to the in-scope mailbox returned 403 ErrorAccessDenied [RAOP] : Blocked by tenant configured AppOnly AccessPolicy settings — the deny rule was live before the group-membership grant had propagated through Graph's enforcement cache. Test-ApplicationAccessPolicy reads the directory synchronously and showed Granted the whole time, which is how we confirmed the config was correct and the 403 was propagation, not misconfiguration.

There is no supported way to force the refresh. It cleared on its own (longer than the optimistic "well under an hour" we first assumed — plan for up to several hours). **Implication:** when adding or removing mailboxes from the scope group, expect a propagation window during which access may be denied. Don't treat a post-change 403 on an in-scope mailbox as a bug until the window has passed and Test-ApplicationAccessPolicy confirms the config.

## Consequences

**Positive:**
- app is least-privilege: one mailbox, read-only. Survives a client security review.
- Filtering and dedup are now correct against how Crown mail *actually* arrives (forwarded, duplicated) — proven end-to-end ingesting 6 real invoices, 5 duplicates correctly skipped, 0 reconcile failures.
- lpg_common removes path hacks and is the clean boundary for the coming two-image split.

**Negative:**
- The scope group is a new object the sync depends on. Deleting it or emptying it silently breaks ingestion (after a propagation delay). Documented here.
- Propagation delay makes scope changes feel slow and non-deterministic; mitigated by the operational note above.
- The forwarding hop (lamppostglobes@outlook.com -> customerservice@) remains a fragility, unchanged from 0016. The real fix is Crown sending directly to a tenant mailbox; still deferred.

## Amends ADR-0016

Supersedes these specifics in 0016: sender-based filtering (Decision/workflow step 3), graph_message_id as primary idempotency key (Schema design not, workflow step 4), Outlook category-tagging (workflow step 5, Negative consequences), and the "future work" framing of the scope lockdown as a ~10-min queued task. 0016's architecture, schema, and product-choice rationale otherwise stand.

## Future work

- **Two-image split.** Separate Dockerfiles/images for the webhook service and the Crown job, both depending on lpg_common; expand build context to repo root. Carries the deferred cloud deploy (Cloud Run job, Cloud Scheduler, IAM, Secret Manager wiring). Next focused session.
- **Crown direct-to-tenant delivery.** Remove the personal-mailbox forwarding hop.
- **Client secret rotation playbook** (carried from 0016).

## References

- ADR-0016 (vendor invoice ingest — the design this hardens)
- Code: scripts/crown_invoice_parser.py, scripts/crown_invoice_writer.py, scripts/sync_crown_invoices.py, lpg_common/
- Azure app: client ID c36883bf-a1b7-4e63-8fc1-c965b32d76ce; tenant fa215d01-a503-4496-ae9f-3ab71e89037e
- Exchange scope group: crown-sync-scope@lamppostglobes.com
