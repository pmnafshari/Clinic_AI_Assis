# Patient Chatbot Architecture and Threat Model

Status: Draft — section 1 complete, sections 2-9 pending
Requirements covered: CHAT-01, CHAT-02
Binds: CHAT-03
Authored: 2026-07-30
Approval: pending — D-18 criteria walkthrough

## 1. Scope and non-goals

This document is the binding architecture and threat model for a future patient-facing
chatbot, CHAT-03, deferred to milestone v2.x. It is the deliverable of Phase 13. No code
ships with it — nothing in this phase adds a table, a route, or a module to the running
app.

CHAT-03 will build a separate, read-only patient chatbot, reachable from the internet via a
tunnel, backed by the clinic's existing local SQLite database. Patients ask questions about
their own record; the chatbot answers from data the clinic already holds, scoped to that one
patient.

**Non-goals**, each with the reason it stays out:

- **Patient self-registration.** Permanently out of scope (D-01). This is not a patient
  portal — every patient account is created by staff, never by the patient.
- **Any change to the staff Flask app.** The patient surface is a separate app (D-13); this
  document does not touch `app/__init__.py`, the staff blueprints, or `web_auth.py`/
  `web_session.py`.
- **Any write path from the patient surface.** The chatbot is read-only throughout — no
  update, append, or add-invoice equivalent exists on the patient toolset (D-10).
- **Free-text SQL or open data access on the patient side.** Listed explicitly in
  `.planning/REQUIREMENTS.md`'s Out of Scope rows ("Self-service patient portal", "Patient
  chatbot with open data access or free-text SQL"). Every patient-side query is one of a
  fixed set of named functions, never an arbitrary query.
- **Email or SMS of any kind.** None exists anywhere in this project. This is why D-04 makes
  a staff-performed credential reissue the only recovery path — there is no "forgot
  password" email link to send.

The document is graded against the three success criteria in `.planning/ROADMAP.md` §Phase
13, quoted verbatim:

1. "The design doc defines patient identity/session as a structurally separate model from
   staff `users` — never a role in the staff table"
2. "The design doc specifies that every data-retrieval call (SQLite and Chroma) is
   hard-filtered by the authenticated patient's own codice_fiscale, enforced at the query
   layer, not just the prompt"
3. "The design doc's threat model explicitly covers: read-only toolset (no
   update/append/add-invoice equivalents), no clinical advice (deflect to dentist), no
   cross-patient visibility, and mandatory per-interaction logging"

Section 8 maps each of these three criteria to the specific section(s) that satisfy it;
D-18 governs the sign-off walkthrough against that mapping.

**How to read this document:** sections 2-5 are the specification — identity/session,
data scoping, the no-advice gate, and the network boundary. Section 6 is the threat
register. Section 7 states what CHAT-03's implementer may and may not change without
recording a deviation.

### Document status and authority

Per D-17, this document is binding with recorded deviation. The security properties it
specifies — structural separation of patient identity from staff auth, query-layer data
scoping, a read-only surface, the no-advice gate, and isolation of the staff app from the
tunnel — are a contract CHAT-03's implementer must hold. Schemas and function signatures may
evolve as CHAT-03 is actually built; the security properties may not evolve silently. Any
deviation from a binding property must be recorded with its rationale, the same discipline
GSD already applies to plan deviations (section 7 states the mechanism).

Per D-16, the document lives in `docs/` rather than `.planning/`. `.planning/` is gitignored
in this repo and never reaches the remote, so a document written there would never reach
CHAT-03's implementer. `docs/` is committed and pushed, so this file survives milestone
archival and is readable by anyone with the repo.

## 2. Patient identity and session model

_PLACEHOLDER — written in a later plan._

## 3. Data scoping and the constrained accessor

_PLACEHOLDER — written in a later plan._

## 4. No-clinical-advice intent gate

_PLACEHOLDER — written in a later plan._

## 5. Network and trust boundary

_PLACEHOLDER — written in a later plan._

## 6. Threat register

_PLACEHOLDER — written in a later plan._

## 7. Deviation policy

_PLACEHOLDER — written in a later plan._

## 8. Decision traceability

_PLACEHOLDER — written in a later plan._

## 9. Open questions for CHAT-03

_PLACEHOLDER — written in a later plan._
