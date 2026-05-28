# ADR-0006: Keep the repo public

**Status:** Accepted
**Date:** 2026-05-27

## Context

When creating the GitHub repository, the choice was public vs. private.
The repo will contain:

- Database schema definitions (DDL)
- Infrastructure-as-code (eventually: Terraform, GitHub Actions)
- Documentation (architecture, decisions, READMEs)
- Possibly: example data, fixtures, test data

The repo will **not** contain:

- Service account keys, API tokens, or credentials
- Real customer data
- Vendor pricing or contractually-sensitive cost data
- Anything under DSG (employer) NDA

## Decision

Keep the repo public.

## Consequences

**Positive:**

- Acts as a portfolio artifact. A Salesforce architect with a public
  GitHub showing thoughtful IaC and database design is a stronger
  professional profile than one without.
- LLM assistants (including future Claude sessions) can read raw GitHub
  URLs directly without any connector or auth dance. Simplifies session
  bootstrapping.
- Free unlimited GitHub Actions minutes, collaborators, etc.
- Forces discipline about not committing secrets — the cost of a slip
  is real and immediate.

**Negative:**

- Anyone can read it. This is only a problem if something sensitive
  gets committed. Mitigated by:
  - A `.gitignore` blocking common secret file patterns
    (`*.json`, `.env`, `*.pem`, etc.) with explicit whitelist for
    legitimate JSON
  - Habit of treating every commit as published the moment it's pushed
  - GCP project IDs are not secrets (they're like phone numbers — knowing
    one doesn't grant access)
- Anyone can fork it. Not a real concern for a learning project.

**Reversal cost:** GitHub allows flipping a public repo to private at
any time. But anything already pushed is assumed scraped — if a secret
leaks, rotating the secret is mandatory regardless of repo visibility
change.
