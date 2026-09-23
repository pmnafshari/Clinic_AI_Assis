# Data protection impact assessment (draft)

> Demo template - see [README](README.md). An adviser completes and signs this; the risks and
> controls below are the ones the code already addresses, as a starting point.

## Why a DPIA

Health data, processed with a language model, reachable by patients over the internet. Any one of
those would usually call for one.

## Risks and controls

| Risk | Control in the code | Residual |
|---|---|---|
| A patient sees another patient's data | every portal read goes through `patient_accessor` with the id from the session; foreign ids are refused and logged as scope violations; tested | low |
| Staff see more than their role needs | `auth.PERMISSIONS` and `ROUTE_POLICY`; every route tested for every role | low |
| Admin sees patient names and codici fiscali | allowed on duplicate review only, by owner decision (2026-09-22); every view audited; `rbac_selftest` 4 and 4b fail if it reaches any other page | accepted by the owner for the demo |
| The model invents an answer | answer-fidelity gate `eval_chat.py` at 1.0; answers built only from the patient's own rows | medium - a model can still err |
| Health data leaves the machine | model, embeddings and index are local; voice demo fenced off and refused behind the tunnel | Cloudflare still sees portal traffic - DPA BLOCKED |
| Data kept after it should be gone | erasure across every store with tombstones re-applied on restore; retention sweep | retention periods are placeholders |
| Audit trail rewritten | append-only triggers; purges audited | someone with file access can still change the database |
| Lost or stolen laptop | FileVault on (checked 2026-09-22); backups encrypted | the live database has no encryption of its own |
| Backups lost with the machine | encrypted archives | **no off-machine copy - BLOCKED on D08** |
| An uploaded document attacks the reader (active PDF, decompression bomb, deep page tree, oversized image) | type from bytes; size, page and pixel limits before decoding; active content quarantined; pypdf limits set explicitly; the worker runs with the network denied by the OS, CPU and file-size limits, a stripped environment and its own temporary folder; its whole process tree is killed past 768 MB (macOS ignores memory rlimits, so the parent measures); at most two workers at once; `memory_guard_selftest` | a new decoder flaw inside the limits; the memory check polls every 5 ms (tolerance 256 MB) |
| Unreviewed or foreign document text reaches the record or a search | extracted text is searchable only after a dentist confirms it; every hit is re-checked in the database for this patient; the same file uploaded for two patients is two files and two reviews; `documents_selftest` S1-S13 | OCR can misread: the dentist confirms what was read, and uncertain pages are marked |
| Instructions hidden inside a document | document text is data: it never changes a query, a permission or a tool | low |
| A dentist sees other patients' records through similar cases (P16) | dentist only; only reviewed visits; shown as case numbers with the month, recorded codes and teeth, and a note excerpt with every patient name, codice fiscale-like code, phone number and e-mail removed; the teaching view has no note text; no patient-app route; every lookup audited with the criteria version; `similar_cases_selftest` | free text can still hold an identifying detail the redaction does not know (a street, a rare condition); criteria and teaching use need the clinical owner and privacy adviser (POL-17, POL-18) |
| Dictation audio | local model only, off by default, audio never kept, the text only fills a form a person confirms | accuracy not approved (P14.T1 BLOCKED) |

## Outcome

**BLOCKED.** Not to be signed off for real data until the controller, the legal basis, the
Cloudflare agreement and the off-machine backup destination are settled.
