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

### 2.1 Table schemas

Two new tables in `db/clinic.sqlite`, created by a new `patient_auth.py` module:

```sql
CREATE TABLE IF NOT EXISTS patient_credentials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    codice_fiscale TEXT NOT NULL UNIQUE REFERENCES patients(codice_fiscale),
    pin_hash TEXT NOT NULL,
    must_change_pin INTEGER NOT NULL DEFAULT 1,
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS patient_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash TEXT UNIQUE NOT NULL,
    codice_fiscale TEXT NOT NULL REFERENCES patients(codice_fiscale),
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
```

| Column | Type | Purpose | Decision |
|---|---|---|---|
| `patient_credentials.pin_hash` | TEXT | staff-set PIN, hashed at rest | D-02 |
| `patient_credentials.must_change_pin` | INTEGER | forces a change before any chatbot access | D-03 |
| `patient_credentials.issued_at` / `expires_at` | TEXT | 7-day validity window on the temp credential | D-02, D-04 |
| `patient_credentials.failed_attempts` / `locked_until` | INTEGER / TEXT | lockout counter and timed auto-unlock cooldown | D-05 |
| `patient_credentials.codice_fiscale` (FK) | TEXT | binds one credential row to exactly one patient record | D-07 |
| `patient_sessions.codice_fiscale` (FK) | TEXT | binds a live session to exactly one patient record | D-07 |

`storage.py:16` runs `PRAGMA foreign_keys = ON` inside `init_db()`, so the FK from
`patient_credentials.codice_fiscale` and `patient_sessions.codice_fiscale` to
`patients(codice_fiscale)` is enforced by SQLite at insert time, not documentation-only — a
credential or session row for a nonexistent patient is rejected by the database itself.

Two structural negatives, stated explicitly: neither table has a `role` column, and neither
has any foreign key or join to `users`. A patient is never a row in `users` and never a
value in `auth.py`'s `VALID_ROLES` (`auth.py:4`) (D-06).

**D-05 numbers chosen here — chosen, not locked.** Only the staff-vs-patient idle-expiry
asymmetry is the locked part of D-05; the specific numbers below are this document's
proposal, open to revision without a deviation record. Patient session idle expiry is
**15 minutes** against `web_session.py`'s staff value of `SESSION_IDLE_MINUTES = 30`
(`web_session.py:9`, Phase 8 D-02) — patients are more likely to be on shared or unmanaged
devices. The failed-attempt threshold is **5** and the auto-unlock cooldown is **15
minutes**, taken directly from `web_auth.py:9-10`'s tested constants
(`LOCKOUT_THRESHOLD = 5`, `LOCKOUT_COOLDOWN_MINUTES = 15`). The lockout shape itself is the
staff pattern reused as a technique, not a new mechanism invented for this design; the
cooldown is a timed auto-unlock so a single mistyped PIN does not strand a remote patient
with no staff member on hand to unlock the account.

PIN hashing uses `werkzeug.security.generate_password_hash` / `check_password_hash`
(scrypt) — the same function pair already used for `users.password_hash`. Session tokens
follow `web_session.py:13-14,21`'s technique: `secrets.token_urlsafe(32)` generated per
login, stored only as its `hashlib.sha256` hash. The technique is reused; the function is
not (see 2.4). Minimum PIN policy: at least 8 characters, reject all-same-digit and
sequential-digit PINs, generated by staff — never chosen by the patient.

### 2.2 Sequence flow: enrolment

1. Staff opens the patient record.
2. Staff issues a PIN through the patient's `patient_credentials` row (created if absent).
3. A `patient_credentials` row is written with `must_change_pin = 1`, `issued_at = now`,
   `expires_at = now + 7 days` (D-02).
4. The PIN is handed to the patient in person or by phone — never by email or SMS, since
   neither exists anywhere in this project.
5. An `auth.log_audit` row (`auth.py:19-27`) records the issuance.

Enrolment is clinic-issued only; there is no self-registration endpoint anywhere in this
design (D-01).

### 2.3 Sequence flow: login and forced credential change

1. Patient submits codice fiscale + PIN.
2. The CF format is validated before any lookup.
3. The `patient_credentials` row for that CF is looked up.
4. `active` is checked, then `expires_at`: an expired temp credential is dead and the login
   returns a "contact the clinic" response — staff reissue is required (D-04).
5. `locked_until` is checked; if still in cooldown, the attempt is refused without touching
   the hash.
6. The submitted PIN is compared with `check_password_hash`, never `==`.
7. On failure, `failed_attempts` increments; at the D-05 threshold, `locked_until` is set to
   now plus the cooldown.
8. On success, a `patient_sessions` row is created (`create_session`-equivalent, own
   module).
9. If `must_change_pin = 1`, every route except the change-credential route redirects there;
   no chatbot route is reachable until the change completes (D-03).
10. After the change, `must_change_pin = 0` and `expires_at` no longer gates access.

Staff reissue is the only password-recovery path in the whole design (D-04) — there is no
"forgot password" flow that emails or texts anything.

### 2.4 Separation rules

D-06's separation rules, stated as a list:

- `patient_auth.py` imports nothing from `web_auth.py` or `web_session.py`.
- A shared session helper parameterised by table name is explicitly rejected. This is named
  as the exact boundary-crossing failure mode CHAT-01 exists to prevent — a single
  `create_session(table_name, ...)` function serving both staff and patients would collapse
  the structural separation this document specifies.
- The patient app has its own `SECRET_KEY` and its own cookie name, distinct from
  `web_session.COOKIE_NAME` (`web_session.py:10`).

One deliberate exception: `auth.log_audit` (`auth.py:19-27`) is reused for the audit trail.
D-06 targets the auth/session mechanism, not the generic audit utility, and a second
parallel log would fragment the trail AUDIT-01/AUDIT-02 depend on. `audit_log.role`
(`storage.py:50-58`) carries no `CHECK` constraint — only `users.role` does
(`storage.py:46`) — so writing `role="patient"` into `audit_log` via the existing
`log_audit()` call violates no database constraint and requires no change to
`auth.VALID_ROLES`.

The D-02 rationale for disallowing the codice fiscale as a temp password, recorded here so a
later implementer does not reintroduce it as a convenience: a real Italian codice fiscale is
derived from name, date of birth, and birthplace. It is a public, derivable value, not a
secret, and it cannot guard an internet-reachable account. This is the reason CF-as-temp-
password is disallowed outright, not merely discouraged.

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
