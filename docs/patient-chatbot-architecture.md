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

D-08's rule stated plainly: the chatbot never touches `storage.py`, `ask.py`, or the Chroma
collection directly. Every read goes through a new `patient_accessor.py` module, and every
function in that module takes `cf` (the session's codice fiscale) as its required first
parameter. This section specifies that module's surface, the two scoping axes it enforces,
the defence-in-depth return check, and the verified Chroma behaviour that shapes it.

### 3.1 Accessor function surface

Four fixed, named query functions — no generic query path, no builder:

```python
# patient_accessor.py — specification sketch, not implementation

def get_demographics(cf, conn):
    return conn.execute(
        "SELECT patient_name, phone FROM patients WHERE codice_fiscale = ?", (cf,)
    ).fetchone()

def get_visits(cf, conn):
    return conn.execute(
        "SELECT visit_date, procedures, next_appointment FROM visits"
        " WHERE codice_fiscale = ? ORDER BY id", (cf,)
    ).fetchall()

def get_next_appointment(cf, conn):
    return conn.execute(
        "SELECT next_appointment FROM visits WHERE codice_fiscale = ?"
        " ORDER BY id DESC LIMIT 1", (cf,)
    ).fetchone()

def get_invoices(cf, conn):
    return conn.execute(
        "SELECT amount, description FROM invoices WHERE codice_fiscale = ?"
        " ORDER BY id", (cf,)
    ).fetchall()
```

| Function | Returns | D-10 category | Excluded columns |
|---|---|---|---|
| `get_demographics(cf, conn)` | patient name, phone | administrative | everything but name/phone |
| `get_visits(cf, conn)` | visit date, procedures performed, next appointment | factual clinical fact | clinical_notes, source_path |
| `get_next_appointment(cf, conn)` | the single most recent next-appointment value | administrative | clinical_notes, procedures |
| `get_invoices(cf, conn)` | amount, description | administrative | visit_id, line_index |

Every one of the four signatures starts `def get_<name>(cf,` and every query body carries a
literal `WHERE codice_fiscale = ?` bound to `(cf,)`. This is D-08's mechanism, applied
uniformly: row scoping is not an option a caller can opt into, it is baked into the only
query each function is capable of running.

The negative list, stated as rules, not suggestions:

- No function accepts a WHERE fragment, a table name, a filter dict, or a raw SQL string
  from a caller.
- No function uses a `*` wildcard column list.
- No function name begins with a write verb, and no INSERT, UPDATE, or DELETE statement
  appears anywhere in the module. D-10 gives the patient toolset no update, append, or
  add-invoice equivalent at all — a write is not denied at runtime, because it does not
  exist as code.
- A generic "authorized query" builder that takes a filter argument from the caller is
  rejected outright, named explicitly here so a later implementer does not reach for it as a
  convenience: a caller could construct an unfiltered query through it. The unsafe query
  must be unwritable, not merely unwritten (D-08).

**Selftest invariant.** `patient_accessor.py --selftest` must include a static textual check,
following this project's per-module selftest convention: read the module's own source with
`inspect.getsource`, then assert that every `get_*` function's body contains the literal
`"codice_fiscale = ?"`, that no call site contains the string `"clinical_notes"`, and that no
`*` wildcard column list appears anywhere in the module. This is what makes "unwritable"
verifiable rather than merely asserted in prose, and CHAT-03 must ship it alongside the
module itself.

**Connection ownership — a document-author call, cross-reference section 9.** CONTEXT.md
left open whether the patient app opens `db/clinic.sqlite` directly or through a narrower
local service boundary. This document resolves it: the patient app opens the database
directly with its own read connection, and `patient_accessor.py` is the only module in the
patient app permitted to hold that connection. No second local service layer sits in front
of it. This matches the project's simplest-correct-code bias — the accessor already provides
the narrowing D-08 requires, and a service layer on top of it would be a second boundary
guarding the same property the first one already guards. Section 9 carries this as an open
question so a reviewer can overturn it during the D-18 walkthrough.

### 3.2 Row and column scoping

D-08 and D-10 scope two different axes, and the accessor must hold both at once:

- **Row scoping (D-08).** The `WHERE codice_fiscale = ?` filter restricts which rows a query
  can return — one patient's rows only, never another's.
- **Column scoping (D-10).** `visits.clinical_notes` never appears in the select list of any
  accessor function, not even for the authenticated patient's own row. This matters because
  `clinical_notes` is where the dentist's provisional reasoning and shorthand live — putting
  that free-text field in front of a language model is the highest-risk path to
  misinterpretation, for a caller who has no clinical training to catch an error.

This is not a new invention. `storage.py:154-170`'s `lookup_clinical()` is the existing
dentist-only sibling of `lookup_patient()` (`storage.py:131-151`) — excluding a field set by
the caller's identity is already an established pattern in this codebase. The accessor
applies the same technique with the patient, rather than the dentist, as the caller whose
identity narrows the columns.

The `cf` used for filtering comes from the `patient_sessions` row established at login
(section 2.3), never from request input. `ask.resolve_cf()` (`ask.py:41-61`) resolves a
patient *name* typed by a caller into a codice fiscale — that is the opposite operation, and
patient code must never call it. Calling it would reintroduce user input as the source of
the filtering value D-08 requires to come from the authenticated session.

### 3.3 Return assertion and its documented limit

D-09's return assertion: before returning, the accessor re-checks every row and every chunk
against the session's `cf`. A mismatch is dropped and raised as a security event via
`auth.log_audit(conn, username, role, action, target, allowed=0)` (`auth.py:19-27`) — never
silently filtered. This project's rule since Phase 7 (RBAC-05) is that denials are loud, and
a dropped row is a denial.

Where the assertion actually bites is not uniform across the two backends it guards. A
parameterised `WHERE codice_fiscale = ?` result cannot, by construction, contain a row for
another CF — SQLite itself guarantees that. On the SQLite path the return assertion is
defence-in-depth against a future JOIN widening the result set, not a control against a
present gap. Its real bite is the Chroma path (section 3.4), where an omitted `where` clause
returns every chunk in the collection regardless of who is asking.

State the limit precisely, not as full coverage: the return assertion catches a broken or
omitted filter at query time. It does not catch a note ingested under the wrong CF at write
time, because both a correctly-scoped query and an incorrectly-attributed chunk read the same
metadata field — the assertion trusts what the metadata says, and a bad ingest writes bad
metadata. Closing that gap is ingest-time provenance verification, explicitly deferred out of
this phase per CONTEXT.md's Deferred Ideas.

### 3.4 Chroma behaviour and the scope of D-08's Chroma clause

Verified this session, directly against `chromadb==1.3.7` — the version pinned in
`requirements.txt` and the version any CHAT-03 implementation should re-verify against before
shipping, since this behaviour has changed across chromadb versions before:

| `where` argument | Observed result | Fails safe? |
|---|---|---|
| correctly scoped (`{"codice_fiscale": "AAA111"}`) | only that patient's chunk(s) | yes |
| non-matching value (`{"codice_fiscale": "ZZZ999"}`) | empty result | yes |
| typo'd key (`{"cf": "AAA111"}`) | empty result | yes |
| empty dict (`{}`) | raises `ValueError` | yes |
| omitted entirely | every chunk in the collection, every patient | **no** |

The live risk is omitting the `where` clause, not malforming it. A non-matching value, a
typo'd key, and an empty dict all fail safe on this pinned version; only omitting `where`
altogether returns everything. `ask.py:184`, inside `answer_meaning()`, calls
`collection.query(query_texts=[question], n_results=k)` with no `where` argument at all —
that is the live gap, and it is exactly why D-08 exists. `collection.get()` carries the same
omission risk, with no `where` argument required to return every id.

Chroma's own history is relevant context, not the current risk: GitHub issue
[chroma-core#1331](https://github.com/chroma-core/chroma/issues/1331) (closed) reported a
non-matching `where` clause leaking every record in an older release; that failure mode does
not reproduce against the pinned 1.3.7 build tested this session. CHAT-03 must re-run this
same five-row verification against whatever chromadb version is pinned at implementation
time rather than assume this table still holds.

**Resolving the D-08/D-10 tension, cross-reference section 9.** `patient_notes`' only
embedded document text is `note.clinical_notes` (`storage.py:186-190`) — the exact field
D-10 excludes from the patient surface. So the MVP accessor in section 3.1 ships zero live
Chroma-querying functions: none of `get_demographics`, `get_visits`, `get_next_appointment`,
or `get_invoices` touches the `patient_notes` collection. D-08's `where={"codice_fiscale":
cf}` clause stands as a forward-looking rule for if a future, non-clinical-notes collection
is ever added to the patient surface — it is not a claim that Chroma access is required for
CHAT-03's MVP. Both decisions hold as written; neither is weakened by the other. This is a
design choice made in this document, not a research-decidable default, and section 9 carries
it as an open question so a reviewer signs off on it explicitly during the D-18 walkthrough.

### 3.5 Sequence flow: a scoped query

1. Patient submits a question over the tunnel (section 5).
2. The session is validated and `cf` is read from the `patient_sessions` row — never from
   the question text.
3. The **intent gate** (section 4) runs on the raw question text. An advice-shaped question
   is deflected here, before anything else in this flow runs.
4. The question is routed to one of the four fixed accessor functions in
   `patient_accessor.py`.
5. The accessor executes its single fixed, CF-filtered query (section 3.1).
6. The return assertion re-checks each returned row's `cf` against the session's (D-09,
   section 3.3).
7. Results and the question are passed to the local model for phrasing, with the "not in
   records" structural refusal — the same guard `ask.py` already uses for staff Q&A — when
   retrieval is empty or insufficient.
8. `log_audit` (`auth.py:19-27`) records the interaction, allowed or denied.

The intent gate (step 3) runs before the accessor is ever reached (step 4) — the gate
naming precedes the accessor naming in this list, and that ordering is load-bearing: an
advice-shaped question never reaches retrieval at all.

## 4. No-clinical-advice intent gate

D-11's rule: no-clinical-advice is enforced by an intent gate that runs before retrieval,
not by a system-prompt instruction. This section specifies the gate's placement, its two
layers, the option that was considered and rejected, and the posture that governs how it is
tuned.

### 4.1 Placement in the request flow

The gate runs on the raw question text immediately after session validation and before any
accessor call, any retrieval, and any model call — step 3 of section 3.5's flow, ahead of
step 4's accessor call by construction.

D-11 explicitly rejects a system-prompt instruction telling the model not to give medical
advice as the enforcement mechanism. It is not an enforcement mechanism and does not satisfy
ROADMAP criterion 3 on its own: the model still receives the question and still generates a
response, so a system prompt is a suggestion the model can ignore, not a gate that blocks
anything. This matches the wider industry direction — OWASP's Top 10 for LLM Applications
treats prompt-level instruction as insufficient against prompt injection and sensitive
information disclosure, and this design applies the same reasoning to clinical-advice
requests.

### 4.2 Layer 1: bilingual keyword and pattern match

Mandatory, always on, zero model cost. The question is normalised (lowercase, accents
stripped) before matching. Three categories, Italian-primary with English secondary:

| Category | Italian triggers | English triggers | Why it triggers |
|---|---|---|---|
| (a) explicit advice-request phrasing | dovrei, devo, è normale che, cosa devo fare | should I, is this normal, do I need | the patient is asking the system to make a clinical judgment call |
| (b) symptom and pain vocabulary | dolore, fa male, gonfiore, sanguina, infiammazione | pain, swelling, bleeding | a patient describing a symptom is implicitly asking for guidance even with no question mark |
| (c) treatment and medication requests | antibiotico, antidolorifico, medicina | what should I take | a request for a treatment or drug recommendation is advice regardless of phrasing |

**[ASSUMED].** 13-RESEARCH.md rates this Italian vocabulary as LOW confidence — synthesised
from general knowledge, not verified against a linguistic corpus or a native speaker. A
native or fluent Italian speaker (clinical staff qualifies) must review and extend this list
before CHAT-03 treats it as an enforcement control. This project already has a precedent for
reviewed domain vocabulary — `dental_shorthand_glossary.json` — and this list should go
through the same kind of review before it ships. Do not treat the table above as
authoritative as written.

### 4.3 Layer 2: semantic similarity (optional, recommended)

Optional for CHAT-03, but recommended: cosine similarity between the question's embedding
and a small curated set of labelled advice-seeking exemplar sentences (Italian and English);
above a threshold, deflect. This reuses the ONNX MiniLM embedder Chroma already loads via
`get_collection()` (`storage.py:173-179`), so the incremental memory cost is near zero and no
new dependency is added — a real constraint under this project's 16GB, one-big-model-at-a-
time rule.

The threshold needs live tuning data and is not ship-ready as specified in this document —
mark layer 2 as needing tuning against real question logs once the patient chatbot has
traffic to tune against. Layer 1 alone is the shipping floor; layer 2 is defence-in-depth on
top of it, not a replacement for it.

### 4.4 Rejected option: repurposing the dental-notes model

Repurposing the fine-tuned `dental-notes` model as an intent classifier was considered and
rejected, for three reasons: a full Ollama round-trip costs seconds per question, which is
slow for a gate that should run before every question is even processed; it needs the 3B
model resident in memory, competing with the answer-synthesis call under this project's
one-big-model-at-a-time rule; and the model is fine-tuned for structured extraction from
third-person dentist notes — a different task and a different input distribution from
classifying a first-person patient question. The third reason is an assumption, not a
measured result: fine-tuning is generally understood to narrow a model's general
instruction-following on out-of-distribution prompts, but this was not tested directly
against this specific model this session.

### 4.5 False-negative posture and deflection response

Design principle, stated on its own: when uncertain, deflect. A false positive costs the
patient one extra step and a "please ask your dentist" message. A false negative risks an
unsupervised local 3B model giving clinical guidance to a patient over the internet. The
asymmetry is not close — every layer above is tuned toward over-triggering, not
under-triggering.

The deflection itself is a fixed, non-generated response string — the model is never invoked
to phrase it — pointing the patient at the clinic. An `auth.log_audit(..., allowed=0)` row
records the deflection, so deflections are visible in the audit trail and the gate's
real-world trigger rate is measurable once the chatbot has live traffic. A deflected question
never reaches retrieval (step 3 precedes step 4 in section 3.5's flow), so no patient data is
loaded for it at all.

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
