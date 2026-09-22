# Privacy and security documents

> **Demo template.** This project is a demo of an Italian dental clinic running on synthetic
> data. Nothing in this folder is legal advice or any real clinic's compliance position. A real
> clinic has these drafts reviewed by its own adviser, fills in the parts marked BLOCKED, and
> signs the processor agreements before a single real record is loaded.

| Document | What it covers |
|---|---|
| [data-flow.md](data-flow.md) | Where each kind of data comes from, where it is stored, who processes it |
| [ropa.md](ropa.md) | Record of processing activities (draft) |
| [dpia.md](dpia.md) | Data protection impact assessment (draft) |
| [retention.md](retention.md) | How long each type is kept, and what the sweep deletes |
| [keys-and-encryption.md](keys-and-encryption.md) | What is encrypted, with which key, and how keys are rotated |
| [incident-runbook.md](incident-runbook.md) | What to do when something leaks or is lost |

## Status of the controls

| Control | State | Where |
|---|---|---|
| Server-side access control on every route | built, tested for every role | `auth.ROUTE_POLICY`, `rbac_selftest.py` |
| Audit trail, append-only, no codice fiscale in identity fields | built, tested | `auth.log_audit`, `storage.AUDIT_APPEND_ONLY` |
| Consent per purpose, with wording version and withdrawal | built, tested | `consent.py`, `consent_texts.json` |
| Access, copy, correction and erasure requests | built, tested | `data_rights.py`, `/data-requests` |
| Erasure across every store, re-applied after a restore | built, tested | `erasure.py`, `backup.restore` |
| Encrypted backups, key rotation | built, tested | `backup.py create`, `backup.py rekey` |
| Retention sweep | built, tested; periods are placeholders | `retention.py`, `retention.json` |
| Encryption of the live database | **not built** - relies on full-disk encryption | see keys-and-encryption.md |
| Controller, processing basis, processor agreements | **BLOCKED** - needs a real clinic and an adviser | ropa.md, dpia.md |
| Off-machine backup destination | **BLOCKED on D08** | [../backup-runbook.md](../backup-runbook.md) |
| Offline copies of the backup and erasure keys | **BLOCKED** - no approved encrypted removable destination | keys-and-encryption.md |

## Review

**Approved for demo use only — not legally reviewed and not production compliance evidence.**
Reviewed 2026-09-22 for consistency with the code and with each other: consent wording
(`consent_texts.json`), retention periods (`retention.json`) and every document in this folder.
Owner decision the same day: admin may see patient names and codici fiscali on duplicate review
only, audited, pinned by tests.

Real use stays closed until every BLOCKED line is resolved. The code guards already refuse to
start in production with synthetic codici fiscali allowed (`codice_fiscale.guard_or_exit`).
