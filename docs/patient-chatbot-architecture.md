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

**Re-verified 2026-08-11 against chromadb==1.3.7 by `chroma_scope_selftest.py` — all four fail-safe rows still hold.**

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

D-12, D-13, and D-14 specify how the patient surface reaches the internet while everything
else stays local. This section states the tunnel topology, the process/port isolation it
depends on, the GDPR exception that isolation creates, and the four controls that stay at
the app layer regardless of what the tunnel provider does.

### 5.1 Tunnel topology

**D-12:** the patient surface is internet-exposed through a secure tunnel — Cloudflare
Tunnel is the named candidate. The database, the models, and all clinical processing stay
strictly local, behind the clinic firewall. The tunnel exposes a surface, not the system —
any design that moves data or model inference outward fails this decision.

```
Patient browser (unmanaged home device)
        |
        | HTTPS — TLS terminated at the Cloudflare edge
        v
  ================ GDPR processor boundary (see 5.3) ================
        |
        | outbound-only connection, initiated by cloudflared
        v
  cloudflared daemon (runs on the clinic machine, same host as both apps)
        |
        | forwards only to the port named in the ingress config (see 5.2)
        v
  +---------------------------+     +------------------------------+
  | Patient Flask app         |     | Staff Flask app (Phase 8)     |
  | own process, own port     |     | binds 127.0.0.1               |
  | (D-13)                    |     | absent from every ingress rule|
  +------------+--------------+     +------------------------------+
               |
               v
     patient_auth session check
               |
               v
     intent gate (section 4)
               |
               v
     patient_accessor.py (section 3)
               |
               v
     db/clinic.sqlite
               |
               v
     log_audit (section 5.5)
```

What crosses the boundary and what does not:

| Crosses the tunnel | Stays local |
|---|---|
| HTTP request bodies (question text) | `db/clinic.sqlite` |
| HTTP response bodies (rendered answer) | the `patient_notes` Chroma collection |
| the patient session cookie | model weights |
| — | every inference call |

First login and the mandatory credential change happen from home, not at the chair — that
is why D-02's 7-day validity window exists: a patient who receives a PIN in person or by
phone needs enough time to complete the forced change from an unmanaged device before the
temp credential expires.

### 5.2 Process and port isolation

**D-13:** the patient chatbot is a separate Flask app, in its own OS process, on its own
port, with its own `SECRET_KEY` and its own cookie name, distinct from
`web_session.COOKIE_NAME` (`web_session.py:10`). It has its own entry point alongside
`run.py` and does not register into `app/__init__.py`. The staff app binds to `127.0.0.1`
and appears in no tunnel ingress rule. Isolation is enforced by process and socket, not by
routing rules or forwarded-header inspection — a header check is spoofable, a separate
listening socket is not.

That isolation claim needs a qualification, stated plainly rather than softened.
`cloudflared`'s ingress config is a human-authored map from a public hostname to
`http://localhost:PORT`, and `cloudflared` runs on the same host as both Flask apps, so it
can reach either app's loopback port. Process separation prevents the two apps from ever
sharing a socket; it cannot prevent a wrong port number in the tunnel's own config from
pointing at the staff app. Earlier project language describing this as unreachable
"regardless of tunnel misconfiguration" is stronger than the mechanism supports. D-13's
actual decision — separate process, separate port — stands unchanged and is correct; the
gap belongs to the ingress config, not to the process boundary, and is carried forward as
threat **T8** in section 6 rather than claimed away here.

Named mitigations for T8: a single-purpose ingress config file that contains only the
patient app's hostname and port, with no staff-app hostname or port ever present in that
file; the config reviewed at setup and after any edit; and periodic verification with
`cloudflared tunnel ingress rule <hostname>`, a real subcommand that reports which rule a
given URL matches. A minimal ingress config:

```yaml
tunnel: <tunnel-id>
credentials-file: /path/to/credentials.json
ingress:
  - hostname: patients.clinic-example.com
    service: http://localhost:PATIENT_PORT
  - service: http_status:404
```

No hostname or port for the staff app appears anywhere in this file — the catch-all
`http_status:404` rule at the bottom is what makes an omission fail closed rather than open.

### 5.3 GDPR processor statement and the offline-first exception

**D-14 control 2:** Cloudflare is a data processor under GDPR for patient traffic transiting
the tunnel, and a Data Processing Addendum (DPA) is required before the tunnel carries
anything real. The clinic is the controller; Cloudflare is the processor. TLS terminates at
Cloudflare's edge, so Cloudflare has plaintext access to request and response bodies in
transit — not a theoretical exposure, a structural one.

Patient traffic crossing a third party is a deliberate, documented exception to this
project's offline-first property, recorded here so a later reader does not treat it as
settled practice for the rest of the system. Every other capability in this project — the
notes model, retrieval, X-ray inference, voice — stays local; the patient chatbot's HTTP
transport is the one path that does not, and it is scoped to exactly that transport.

Open item, not settled: whether Cloudflare's DPA is available to free-tier tunnel customers
on the same terms as paid tiers was not conclusively confirmed during research. Free-tier
DPA access needs direct confirmation from Cloudflare before CHAT-03 ships, not left as an
assumption at document time.

**D-14 control 3, the no-real-data precondition,** is a hard gate on going live, not a
recommendation: the tunnel must not carry real patient data until the milestone-level switch
to offline storage plus encryption has happened. Development and UAT run on fake data only.

### 5.4 App-layer controls

| Control | What it does | Why it is not outsourced to the tunnel |
|---|---|---|
| Login throttling | Per-account and per-source-IP throttling in `patient_auth.py`: threshold 5, cooldown 15 minutes, timed auto-unlock, same numbers as `web_auth.py:9-10`'s tested constants (section 2.1) | The D-05 lockout must not depend on infrastructure this repo does not control; granular rate-limiting rules are historically a paid Cloudflare WAF feature, so the free tier may not provide equivalent throttling at all |
| GDPR processor / DPA | Cloudflare is named as a processor and a DPA is required before real traffic — see 5.3 | The tunnel provider's own terms of service are not a substitute for the clinic's own GDPR obligations as controller |
| No-real-data precondition | The tunnel carries fake data only until the offline+encryption milestone switch — see 5.3 | The tunnel's TLS and access controls do not change what the clinic is permitted to expose while real data is not yet protected at rest |
| Tunnel-independent audit | Every interaction is logged at the app layer via `log_audit` — see 5.5 | Tunnel access logs live with the provider, expire on their schedule, and cannot be joined to a patient session |

The patient app needs its own default-deny guard: a `before_request` handler with its own
whitelist, mirroring the structure at `app/__init__.py:70-83` and `app/__init__.py:13`
without importing it, so a newly added route is protected by default rather than by
remembering to protect it. The patient session cookie carries `HttpOnly` and
`SameSite=Strict`, matching `app/__init__.py:24`, and every patient form carries CSRF
protection via Flask-WTF. These matter more here than on the staff app because the patient
surface is internet-reachable, not localhost-only.

### 5.5 Audit logging of patient interactions

**D-14 control 4:** every patient interaction is logged at the app layer, allowed or denied,
using `auth.log_audit` (`auth.py:19-27`) and the existing `audit_log` table — not a second
parallel log, which would fragment the trail AUDIT-01 and AUDIT-02 depend on. Tunnel access
logs live with the provider, expire on the provider's schedule, and cannot be joined to a
patient session — that is why per-interaction audit belongs at the app layer, independent
of what the tunnel logs.

The column mapping for a patient row: `username` carries the authenticated codice_fiscale;
`role` carries the literal string `role="patient"`; `action` names the interaction — for
example `patient_query`, `patient_deflect`, `patient_login`, `patient_scope_violation`;
`target` names the accessor function or the deflection category; `allowed` is 1 for a served
answer and 0 for a deflection or a scope violation.

The schema fact that makes this legal: `audit_log.role` (`storage.py:50-58`) carries no CHECK
constraint. `users.role` (`storage.py:46`) does —
`CHECK(role IN ('dentist', 'assistant', 'admin'))` — but `audit_log` does not. Writing
`role="patient"` into `audit_log` via the existing `log_audit()` call violates no database
constraint and requires no change to `auth.VALID_ROLES` (`auth.py:4`).

The corollary rule: `"patient"` must stay absent from `VALID_ROLES` and from `PERMISSIONS`
(`auth.py:7-11`), because a patient is an audited identity, never an authorised staff role —
adding it to either would silently reverse D-06's structural separation.

Close with the log-before-respond rule: the `log_audit` write sits on the same code path as
the response, so a served answer cannot exist without its audit row — the same
log-before-write discipline this project already uses for undo.

## 6. Threat register

D-15 requires the deliverable to carry a full enumerated threat register with a named
mitigation per threat, not a short ADR. This section is the register criterion 3 is graded
on.

### 6.1 Method and trust boundaries

STRIDE is used as a per-row tag on a single table, not a full system-wide walkthrough. This
satisfies D-15's named-mitigation-per-threat requirement, gives D-18's criteria walkthrough
concrete rows to trace against each roadmap success criterion, and stays lighter than a
methodology write-up — matching this project's simplest-correct bias.

| Boundary | What crosses | Assumed trustworthy |
|---|---|---|
| internet to tunnel edge | HTTPS request/response bodies | no — public internet |
| tunnel edge to cloudflared on the clinic host | decrypted HTTP, GDPR processor boundary (§5.3) | Cloudflare as a named, DPA-bound processor |
| cloudflared to the patient Flask app port | plain HTTP over loopback | yes — same host, config-gated (§5.2) |
| patient app to `patient_accessor.py` | the session's codice_fiscale, the question text | yes — same process |
| accessor to `db/clinic.sqlite` | one parameterised, CF-filtered SQL query | yes — same process, D-08's fixed functions |
| patient app to the local model | retrieved rows, the question | yes — same host, no network hop |

The one assumption that matters: everything below the accessor is trusted, so the accessor
is the enforcement choke point — the same role `authorize()` plays for staff.

### 6.2 Threat register

| ID | Threat | STRIDE | Attack scenario | Mitigation | Residual risk / limit | Criterion |
|---|---|---|---|---|---|---|
| T1 | Prompt injection attempting cross-patient exfiltration | Information Disclosure | Patient submits a crafted question instructing the model to ignore instructions and reveal another patient's data | Accessor CF-scoping (D-08, §3.1) means no other patient's rows or chunks are ever loaded into context — nothing exists in the prompt for an injection to exfiltrate | Model could still fabricate plausible but false content; mitigated, not eliminated, by the "not in records" structural refusal already used for staff Q&A (§3.5 step 7) | 2 |
| T2 | Cross-patient visibility via a broken or omitted Chroma filter | Information Disclosure | A future accessor function queries Chroma without a where clause, or with a malformed one, and returns another patient's chunk | D-08's mandatory where={"codice_fiscale": cf} (§3.1) plus D-09's return assertion (§3.3) drop and log any row or chunk whose cf does not match the session | Does not catch a chunk ingested under the wrong codice_fiscale at write time (§3.3's documented limit) — the ingest-time provenance gap is explicitly deferred out of this phase (§9) | 2, 3 |
| T3 | An advice-shaped question reaches the model | Information Disclosure / Elevation of Privilege | Patient asks "should I take antibiotics for this swelling" and the question is passed straight to retrieval and the model | D-11's two-layer pre-retrieval gate (§4.1-4.3): mandatory bilingual keyword and pattern match plus optional semantic similarity, both running before any accessor or model call | The gate's own false-negative rate is unvalidated without live question data; the Italian trigger vocabulary is marked [ASSUMED] in §4.2 and needs native-speaker review before shipping — flagged for post-launch monitoring | 3 |
| T4 | Attempted write through the patient chatbot | Tampering | Patient's question is phrased as an instruction to update a record, add an invoice line, or append a note | D-10, §3.1: no write function exists anywhere in patient_accessor.py — a write is not denied at runtime, it is absent as code, the same unwritable-not-unwritten standard the read path is held to | None known — no code path exists to attempt a write against; the selftest in §3.1 statically asserts no INSERT, UPDATE, or DELETE appears in the module | 3 |
| T5 | Credential stuffing or brute force on the internet-exposed patient login | Spoofing | Automated attempts against many codice_fiscale plus PIN combinations from the internet | D-05 lockout (§2.1: threshold 5, cooldown 15 minutes) plus D-14 control 1 app-layer throttling (§5.4), independent of the tunnel provider | Lockout is per-account; a distributed low-and-slow attack spread across many accounts is not fully addressed by a per-account threshold | — |
| T6 | Future reintroduction of the codice fiscale as a temporary password, for convenience | Spoofing | A later maintainer adds CF-as-PIN as a shortcut because it removes a manual staff step | D-02's rationale recorded in §2.1 (a CF is derived from name, date of birth, and birthplace — a public value, not a secret) plus D-17's binding-with-recorded-deviation discipline | Relies on CHAT-03's implementer reading and following the document; not enforced by any runtime check | 1 |
| T7 | Tunnel provider as adversary or compromised dependency with plaintext access to traffic | Information Disclosure | Cloudflare's edge, or a party who compromises it, reads request and response bodies in transit since TLS terminates there | D-14 control 2 names Cloudflare as a GDPR processor requiring a DPA (§5.3); D-12 keeps the database, models, and all clinical processing local (§5.1) — only HTTP request and response bodies cross the tunnel | Accepted, documented exception to offline-first (§5.3); scope-limited since clinical DB and model traffic never leave the LAN; the no-real-data precondition (§5.3) keeps this residual theoretical until go-live | — |
| T8 | Staff app reachable through tunnel misconfiguration | Elevation of Privilege | The cloudflared ingress config is edited to point a hostname at the staff app's port, or a typo sends the patient hostname there | D-13 process/port separation (§5.2) plus a single-purpose ingress config containing only the patient app's hostname and port, verified periodically with cloudflared tunnel ingress rule (§5.2) | Isolation depends on the ingress config being correct, not on process separation alone — a wrong port number in that human-authored file is not prevented by either app's own code | — |
| T9 | An interaction is not logged | Repudiation | A code path returns an answer or a deflection without a corresponding audit_log row | D-14 control 4: log_audit (auth.py:19-27) sits on the same code path as the response, log-before-respond (§5.5), reusing the existing audit_log table rather than a second parallel log | None known, provided the log_audit call and the response stay on the same code path — the same log-before-write discipline this project already applies to undo | 3 |
| T10 | Model output leaking or fabricating data the accessor correctly filtered | Information Disclosure | Model free-associates or hallucinates a plausible-sounding fact not present in the retrieved, correctly-scoped context | The "not in records" structural refusal (§3.5 step 7), the same guard already built for staff Q&A in ask.py, triggers when retrieval is empty or insufficient | Residual risk is inherent to any generative model; reduced, not eliminated, by the structural refusal | — |
| T11 | Session token compromise via XSS or cookie theft | Information Disclosure / Spoofing | An attacker reads the patient session cookie through a script injection or physical or shared-device access | HttpOnly and SameSite=Strict cookie flags on the patient session cookie (§5.4), matching app/__init__.py:24; CSRF protection via Flask-WTF on every patient form (§5.4) | Standard browser-security residual; device-level compromise on a shared or unmanaged home device is out of scope for an app-layer control | — |
| T12 | Weak or guessable staff-set PIN | Spoofing | Staff issues a short or sequential PIN that is easy to guess or shoulder-surf | Minimum PIN policy enforced at issuance in patient_auth.py (§2.1): at least 8 characters, reject all-same-digit and sequential-digit PINs, hashed with werkzeug scrypt (§2.1), never compared with == | Policy strength is a document-time choice; a determined guesser with unlimited attempts is still bounded by D-05's lockout (§2.1), not by PIN strength alone | — |
| T13 | Expired or reissued credential still accepting a login, or a stale patient session surviving a staff revocation | Spoofing | Patient's temp PIN passes 7 days unused but the expiry check is skipped, or staff revokes a patient's access but an already-issued session keeps working | D-04's expires_at check and D-05's active flag (§2.1, §2.3) at login, plus deletion of the patient's patient_sessions rows on revocation | Revocation is only as fast as the next request the patient's existing session makes — a session already mid-request when revoked completes that one request | — |
| T14 | A future maintainer adds a shared session helper parameterised by table name, or adds "patient" to VALID_ROLES | Elevation of Privilege | A later change collapses the structural separation D-06 requires, for code-reuse convenience | D-06 (§2.4) and the §7 deviation policy, plus a selftest asserting "patient" is absent from auth.VALID_ROLES | Relies on the deviation policy being followed and the selftest being run; not prevented by a database constraint | 1 |

The table above covers all four of criterion 3's graded categories: T4 (read-only), T3 (no
clinical advice), T2 (no cross-patient visibility), T9 (mandatory per-interaction logging).

The controls below are what CHAT-03 must implement — none of them are implemented in this
phase, which produces a document only.

| Category | Control specified | Section |
|---|---|---|
| V2 Authentication | Staff-issued PIN hashed with werkzeug scrypt, forced change on first login, 7-day expiry | §2.1-2.3 |
| V3 Session Management | Server-side hashed-token sessions in patient_sessions, shorter idle expiry than the staff 30 minutes | §2.1 |
| V4 Access Control | Constrained accessor, read-only toolset, "patient" kept absent from VALID_ROLES and PERMISSIONS | §3, §5.5 |
| V5 Input Validation | Codice fiscale format validated before lookup; intent-gate input normalised before matching | §2.3, §4.2 |
| V6 Cryptography | PIN and session-token hashing via werkzeug scrypt and hashlib.sha256, never a hand-rolled scheme | §2.1 |
| V13 API and Web Service | CSRF protection via Flask-WTF and HttpOnly/SameSite=Strict cookie flags on every patient form and session cookie | §5.4 |

### 6.3 Criterion coverage

| Criterion | What it requires | Threat rows | Sections |
|---|---|---|---|
| 1 | Patient identity/session structurally separate from staff users — never a role in the staff table | T6, T14 | §2.1, §2.4 |
| 2 | Every data-retrieval call hard-filtered by the authenticated patient's own codice_fiscale, enforced at the query layer | T1, T2 | §3.1-3.3 |
| 3 | Threat model explicitly covers read-only toolset, no clinical advice, no cross-patient visibility, mandatory per-interaction logging | T3, T4, T2, T9 | §3.1, §4, §5.5 |

This table is what D-18's criteria walkthrough reads from; section 8 carries decision-level
traceability separately.

## 7. Deviation policy

D-17 makes this document binding, with recorded deviation as the only permitted departure
from a security property. This section states which properties are held to that standard and
how a deviation is written down.

### 7.1 What is binding

Five security properties are binding on any future implementation of this document. Each
must hold as written; the document does not permit a silent variant.

1. **Structural separation of patient identity from staff auth** (§2) — a patient is never a
   row in `users` and never a value in `auth.VALID_ROLES`; `patient_auth.py` shares no
   function with `web_auth.py` or `web_session.py`.
2. **Query-layer scoping to the authenticated codice_fiscale** (§3) — every accessor function
   filters by the session's `cf`, never by a value taken from request input.
3. **The read-only patient surface with no write functions** (§3.1) — no `INSERT`, `UPDATE`,
   or `DELETE` statement and no write-verb function name exists anywhere in the patient
   toolset.
4. **The pre-retrieval no-advice gate** (§4) — an advice-shaped question is deflected before
   any accessor call, any retrieval, and any model call; a system-prompt instruction alone
   does not satisfy this.
5. **Staff-app isolation by separate process and port** (§5.2) — the patient surface and the
   staff app never share a socket, a `SECRET_KEY`, or a cookie name.

What is not binding, and may evolve freely as CHAT-03 is actually built: exact table and
column names, accessor function signatures, the idle-expiry and lockout numbers chosen in
§2.1, the intent-gate vocabulary and thresholds in §4.2-4.3, and the section ordering of this
document itself. The line is explicit: schemas and function surfaces may change; the five
security properties above may not change silently. This is D-17's rule stated as a working
line an implementer can apply without re-reading the whole document.

### 7.2 How to record a deviation

A deviation from one of the five binding properties is permitted only if it is written down.
The record has six fields:

| Deviation | Property affected | Reason | Alternative control | Date | Approved by |
|---|---|---|---|---|---|
| _(example row — delete before the first real entry)_ | | | | | |

Each deviation record is appended to a `### Deviations` subsection of this document, in the
repo — not in a plan file, not in `.planning/` — so it travels with the contract itself
rather than living in a milestone-scoped artifact that gets archived away from CHAT-03's
implementer.

The rule in one line: a documented deviation is allowed, an undocumented deviation is not.
This is the same discipline GSD already applies to plan-level deviations (an architectural
change requires a recorded decision), applied here to a cross-milestone document instead of a
single plan. Per D-17, an implementer who cannot follow one of the five binding properties as
written must add a row to this table before shipping the deviating behavior, not after.

## 8. Decision traceability

### 8.1 Decisions to sections

| Decision | Summary | Section | Threat rows |
|---|---|---|---|
| D-01 | Clinic-issued patient accounts; no self-registration path exists | §1 Scope and non-goals | — |
| D-02 | Staff-set PIN, 7-day validity; codice fiscale explicitly disallowed as the temp credential | §2.1 Table schemas | T6 |
| D-03 | Forced PIN change on first login, before any chatbot route is reachable | §2.3 Sequence flow: login and forced credential change | — |
| D-04 | Temp credential expires at 7 days; staff reissue is the only recovery path | §2.3 Sequence flow: login and forced credential change | T13 |
| D-05 | Shorter patient idle expiry than staff's 30 minutes, with a timed auto-unlock lockout cooldown | §2.1 Table schemas | T5 |
| D-06 | Structural separation: separate tables and module, no shared session function, no patient role in `users` | §2.4 Separation rules | T14 |
| D-07 | Patient credential row bound to `patients.codice_fiscale` by an enforced foreign key | §2.1 Table schemas | — |
| D-08 | Constrained accessor module: every query CF-filtered by construction, an unfiltered query is unwritable | §3.1 Accessor function surface | T1, T2 |
| D-09 | Return assertion re-checks every row and chunk against the session's cf, drops and logs a mismatch | §3.3 Return assertion and its documented limit | T2 |
| D-10 | Exposed surface is administrative data plus factual clinical facts only; `clinical_notes` excluded; read-only throughout | §3.2 Row and column scoping | T4 |
| D-11 | No-clinical-advice enforced by an intent gate that runs before retrieval, not by a system-prompt instruction | §4.1 Placement in the request flow | T3 |
| D-12 | Patient surface is internet-exposed via a secure tunnel; database, models, and clinical processing stay strictly local | §5.1 Tunnel topology | T7 |
| D-13 | Separate Flask app, own process and port; the tunnel points only at that port | §5.2 Process and port isolation | T8 |
| D-14 | Four app-layer controls: login throttling, GDPR processor statement, no-real-data precondition, mandatory per-interaction audit | §5.4 App-layer controls | T5, T7, T9 |
| D-15 | Deliverable is a full spec plus enumerated threat register, not a short ADR | §6. Threat register | — |
| D-16 | Document lives in `docs/`, committed and pushed — not `.planning/` | §1's "Document status and authority" | — |
| D-17 | Document is binding, with recorded deviation, as the only permitted departure | §7. Deviation policy | T6 |
| D-18 | Approval is a criteria walkthrough sign-off, one roadmap criterion at a time, with explicit human sign-off | §8.2 Success criteria walkthrough | — |

### 8.2 Success criteria walkthrough

D-18 makes this walkthrough the approval mechanism for the phase — the document is reviewed
against the three ROADMAP §Phase 13 success criteria one at a time, each traced to the
sections and threat rows that satisfy it.

1. **Patient identity/session structurally separate from staff `users`, never a role in the
   staff table.** Satisfied by §2.1 (the two `patient_*` table schemas, no `role` column, no
   FK to `users`) and §2.4 (separation rules: no shared function with `web_auth.py` or
   `web_session.py`, the one deliberate `auth.log_audit` exception stated and justified).
   Threat rows T6 and T14 name the failure modes this criterion guards against.

2. **Every data-retrieval call, SQLite and Chroma, hard-filtered by the authenticated
   patient's own codice_fiscale, enforced at the query layer, not just the prompt.**
   Satisfied by §3.1 (every accessor function's fixed, CF-filtered query), §3.2 (the row and
   column scoping axes held together), and §3.3 (the return assertion and its documented
   limit). Threat rows T1 and T2 name the failure modes.

3. **The threat model explicitly covers: read-only toolset, no clinical advice, no
   cross-patient visibility, mandatory per-interaction logging.** Satisfied by §3.1 (no write
   function exists in the accessor), §4 (the pre-retrieval intent gate), and §5.5 (audit
   logging on the same code path as the response). Threat rows T2, T3, T4, and T9 cover the
   four categories in order.

### Sign-off

| Criterion | Section traced | Approved | Date |
|---|---|---|---|
| 1 | §2.1, §2.4 | approved | 2026-07-30 |
| 2 | §3.1-§3.3 | approved | 2026-07-30 |
| 3 | §6.2 (T2, T3, T4, T9), §6.3 | approved | 2026-07-30 |

## 9. Open questions for CHAT-03

Five items are enumerated here rather than resolved. Where sections 3 and 5 already state a
working default, that default is recorded here as reversible, not as a settled decision.

1. **D-08's Chroma clause versus D-10's free-text exclusion.** Known: `patient_notes`' only
   embedded document text is `note.clinical_notes` (`storage.py:186-190`), which D-10
   excludes. Unclear: whether the MVP ships zero Chroma-querying functions with D-08's clause
   standing as a forward-looking rule, or whether CHAT-03 takes on an ingest change adding a
   second non-clinical collection. Decided at the 2026-07-30 D-18 walkthrough: the reviewer
   kept the recommended default — the MVP ships zero Chroma-querying functions, and D-08's
   clause stands as forward-looking, matching §3.4. Decides: settled by the reviewer at
   sign-off; CHAT-03 planning only revisits this if it wants the alternative (an ingest change
   adding a non-clinical collection).

2. **How strongly to qualify D-13's isolation claim.** Known: `cloudflared`'s ingress is a
   human-authored hostname-to-port map on the same host as both apps. Unclear: whether the
   single-purpose ingress config plus periodic `cloudflared tunnel ingress rule` check is
   sufficient, or whether CHAT-03 should add a startup assertion. Default: the §5.2
   mitigations, with the gap carried as T8. Decides: reviewer. Wanted before CHAT-03 planning
   starts.

3. **Conversation and transcript retention.** Known: nothing is stored today and D-14.4 only
   mandates per-interaction audit rows. Unclear: whether patient chat history is retained at
   all, for how long, and who may read it — GDPR-relevant. Default: none — this was raised
   and left undecided in discussion. Decides: CHAT-03 planning, with a GDPR review. Answerable
   during CHAT-03 planning.

4. **Behaviour when the tunnel is down.** Known: the patient surface becomes unreachable; the
   staff app and all local processing are unaffected. Unclear: the patient-facing degradation
   story. Default: none — operational rather than architectural. Decides: CHAT-03 planning.
   Answerable during CHAT-03 planning.

5. **Direct database access versus a narrower local service boundary.** Known: §3.1 states
   the patient app opens `db/clinic.sqlite` directly with `patient_accessor.py` as the only
   module holding that connection. Unclear: whether a separate local service boundary is
   worth the added moving parts. Default: direct connection, accessor-only, enforced by the
   §3.1 selftest — the simpler option. Decides: reviewer, since CONTEXT.md marks it
   undecided. Wanted before CHAT-03 planning starts.
