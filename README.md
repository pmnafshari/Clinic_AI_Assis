# Dental AI Assistant

A private, offline-first assistant for a dental clinic, built around the clinic's own
fine-tuned model rather than a general chatbot.

It reads messy dentist notes and turns them into schema-valid structured data, sorts
incoming clinic files on its own, stores everything for question-answering and record
edits, and gives staff and patients their own web surfaces on top of it. Everything that
touches clinical data runs locally: a small model through Ollama, SQLite, and a local
embedding index. No cloud API is in the clinical path.

**Development uses fake data only.** Nothing here has seen a real patient record.

## What is built

Three separate Flask apps, each with its own database posture and its own access rules.

| | Port | Who | What it does |
|---|---|---|---|
| **Staff CRM** | `5000` | Signed-in staff | Note intake and extraction, patients and visits, invoices, uploads, Q&A, record edits through a confirm-diff, reports, appointments and the day agenda, user admin |
| **Patient portal** | `5001` | Signed-in patients | Assistant chat scoped to their own records, profile, appointment requests and cancellation. Bilingual Italian / English |
| **Public site** | `5002` | Anyone | Clinic website — services, doctors, clinic, contact — plus a public assistant that answers from clinic information only |

The staff app is never exposed to the internet. The patient app is the only one designed to
go behind a tunnel, and refuses to start if its ingress configuration is wrong.

## Quickstart

Requires Python 3.12 and [Ollama](https://ollama.com/download).

```bash
# 1. environment
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. the model
ollama pull llama3.2
# dental-notes is the clinic's fine-tuned model. Its weights are ~2GB and are NOT
# in this repo — train it in Colab first, then put the .gguf next to Modelfile:
ollama create dental-notes -f Modelfile

# 3. database and dev accounts
.venv/bin/python seed_users.py

# 4. run it — one terminal each
ollama serve
.venv/bin/python run.py                                   # staff    :5000
PATIENT_COOKIE_SECURE=0 .venv/bin/python patient_run.py   # patient  :5001
.venv/bin/python site_run.py                              # public   :5002
```

`seed_users.py` creates three dev accounts — `dentist`, `assistant`, `admin` — with
throwaway passwords printed in that file. They are development credentials and the script
says so on every run: change them before any real data goes near this.

There are no patients until notes are filed. `make_fixtures.py` writes a set of sample
notes, spreadsheets and images into `drop/`; upload them through the staff app and the
extraction pipeline creates the patients:

```bash
.venv/bin/python make_fixtures.py     # sample files into drop/
.venv/bin/python watcher.py           # optional: file them automatically
```

A patient signs in to `:5001` with a codice fiscale and a clinic-issued PIN. Staff issue
one from the patient's record in the staff app.

**On a Mac without FileVault, `run.py` will refuse to start.** It reads patient data, so it
checks for full-disk encryption first. Turn FileVault on, or set `DISK_GUARD_DISARMED=1`
for a fake-data demo — it prints a loud warning either way.

## The notes model

The core of the project. A messy Italian or English dentist note goes in; a validated
`DentalNote` comes out.

```
note text ──> dental-notes (Llama 3.2 3B + LoRA, via Ollama)
          ──> extract_note.py  ──> DentalNote (pydantic)  ──> SQLite + Chroma
```

Training runs on the free Colab GPU — `notebooks/train_notes_lora.ipynb`, using
`notes_train.jsonl` (165 rows, committed). **The trained weights are not in this repo**: a
GGUF is around 2 GB, so `Modelfile` points at a file you produce. Download the result to
the repo root, check the filename matches the `FROM` line in `Modelfile`, then:

```bash
ollama create dental-notes -f Modelfile
ollama run dental-notes "pt mario rossi, rct on 26 done, mild caries on 27, fu in 2 weeks"
```

`Modelfile` and the notebook's `SYSTEM_PROMPT` are asserted identical by the test suite —
they drift apart silently otherwise, and the model then gets trained on one prompt and run
on another.

**Current accuracy**, `eval_notes.py` over 34 held-out notes:

| field | |
|---|---|
| patient_name, codice_fiscale, visit_date | 1.00 |
| next_appointment | 0.97 |
| phone | 0.94 |
| procedures | 0.88 |
| invoices | 0.79 |
| **aggregate** | **0.94** |

## File intake

Files dropped into `drop/` are picked up by `watcher.py`, classified by `sort_files.py`,
and filed under `sorted/`. A note that extracts cleanly is filed; one that fails reaches
`needs_review` with a reason attached, and shows a badge in the staff UI.

An unrecognised procedure code is **flagged for review, never rejected** — refusing the
note would lose a real clinical record over a vocabulary gap.

## Access control

Roles map to capability sets in `auth.py` — a plain dict, no policy engine.

- **dentist** — full clinical access, appointments, patient PINs
- **assistant** — notes, invoices, uploads, appointments, patient PINs; no record editing
- **admin** — user management only, and **cannot open a patient record at all**

Withheld means *absent from the response*, never hidden with CSS, and the test suite
asserts that per role. Every mutating action writes an audit row naming the actor.

## Privacy posture

- **Fake data only** through development. GDPR work — encryption at rest, a processor
  agreement for any tunnel — lands before real patient data does.
- The patient portal reaches records only through a scope-checked accessor. A mismatched
  codice fiscale returns nothing and logs a `patient_scope_violation`.
- **Voice is a fenced demo, off by default.** It sends audio to Deepgram and ElevenLabs,
  which leaves the machine — for real patient speech that is Article 9 health data needing
  an agreement the clinic does not have. It requires `VOICE_DEMO=1` and refuses to arm at
  all when the app is internet-facing. The intended production path is faster-whisper and
  Piper, offline.
- No vendor API key ever reaches the browser, and no recording is written to disk.

## Tests

```bash
./run_selftests.sh          # fast: every module's --selftest, no servers, no model
```

That must exit 0 before any commit. Four slower gates need real services, and each covers
something the fast suite cannot reach — a stubbed model and a hand-built form body hide
exactly the defects these catch:

| | Covers |
|---|---|
| `e2e_chat_walk.py` | the patient chat, through real Chromium and a real model |
| `e2e_intake_walk.py` | note upload → extraction → SQLite, end to end |
| `e2e_voice_walk.py` | the assistant voice paths, including every failure mode |
| `eval_chat.py` / `eval_notes.py` | answer fidelity, and model accuracy |
| `shot_pages.py` | every page fits at 390px and 1440px with no horizontal scroll |

Install the browser tooling with `.venv/bin/pip install -r requirements-dev.txt` then
`.venv/bin/python -m playwright install chromium`. It is development-only and never goes on
the clinic machine.

## What this does not do

Stated plainly, because a demo that overstates itself is worse than a small one:

- **No payments.** Invoices carry no status, so nothing here proves money was collected.
- **No patient-growth reporting.** Patient records carry no created date, so it cannot be
  computed and is not shown.
- **Patients request appointments, they do not book them.** There is no opening-hours table
  and no dentist roster, so a slot picker would have to invent availability. A patient names
  a day and a period; staff assign the real slot when they confirm.
- **X-ray analysis is not built.** It is on the roadmap, not in the code.
- **A known model defect is open**: Italian terms that map to a dissimilar code — `igiene`
  and `pulizia` → `prophy`, `panoramica` → `opg` — translate correctly as the first
  procedure in a note and come back untranslated as the second. Training coverage for that
  position is in place; the fix lands on the next retrain. The term is flagged for review
  rather than silently mistranslated.

## Layout

```
app/          staff CRM (blueprints, templates)
patient_app/  patient portal
site_app/     public clinic site
shared/       design tokens and components used by all three
notebooks/    Colab training
docs/         architecture notes
*_selftest.py per-module test suites
e2e_*.py      browser-driven walks
```

`Dental_AI_Roadmap.md` holds the original long-range plan.
