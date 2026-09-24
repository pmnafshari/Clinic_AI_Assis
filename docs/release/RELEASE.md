# Release scope - trial version

**This is a trial version on synthetic data. It is not approved for production or for real
patient data.** Real data stays forbidden until every gate named below is closed by the people who
own it (owner, clinical owner, legal/privacy). A passing test suite is not a release approval.

Status 2026-09-24: operations tooling built and tested (P17). Owner agreement to this scope, the
human review and the deployment environment are still open.

## Enabled in the trial

| Area | What works | Where |
|---|---|---|
| Notes | typed notes, uploaded notes held for a dentist's review before they join the record | staff app |
| Patients | records, visits, duplicates and merge, erasure, export, consent | staff app |
| Appointments | booking, requests from the portal, confirmation, month calendar, roster rules | staff + patient apps |
| Billing | invoices in cents, installments, manual payments, refunds, reconciliation | staff app |
| Stock | items, movements, low-stock list | staff app |
| Reminders | planned and queued; **not sent** (no provider, see below) | staff app |
| Next-visit summary | local draft from the patient's own notes, dentist approval | staff app |
| Documents | PDF/image/text read in a sandbox, dentist review, patient-scoped search | staff app |
| Patient portal | sign-in, own appointments, billing, consent, data requests, assistant chat on own records | patient app |
| Public site | clinic pages and a public assistant that answers from clinic information only | site app |
| Operations | health check, backups with restore drill, local CI, load/restart check | command line |

## Off by default (a switch turns it on; each needs an owner decision first)

| Feature | Switch | Why it is off |
|---|---|---|
| Similar past cases | `CLINIC_SIMILAR_CASES=1` | matching rules are an unapproved draft (clinical owner) |
| Local speech-to-text for dictation | `CLINIC_STT_ADAPTER=local` | accuracy not clinically approved |
| Public-site voice demo | `VOICE_DEMO=1` | demo only |
| Messaging / payments / telephony | `CLINIC_*_ENABLED` + provider + kill switch + spend cap | no provider chosen, no budget |

## Blocked (nothing in this release does these)

- Sending any message, taking any payment, placing or answering any call (no provider, no budget).
- Reading a summary aloud (voice licence unresolved).
- Image similarity and voice search over documents (no model, licence or dataset).
- Interpreting shorthand procedure codes for patients (no clinical glossary owner).
- X-ray analysis of any kind (separate legal and clinical gate).
- Any hosted service: CI, monitoring delivery, off-machine backups (owner decisions).

## Before real data (not done)

Owner sign-off of this scope; the human review of every screen; clinical sign-off of the note model,
summaries and similar cases; legal/privacy approval of the records in `docs/privacy/`; an approved
deployment environment with TLS, firewall and access control; off-machine backups; an alert
recipient; changed passwords (`seed_users.py` accounts are dev-only); `CLINIC_ALLOW_SYNTHETIC_CF=0`.
