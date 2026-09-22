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
| Staff see more than their role needs | `auth.PERMISSIONS` and `ROUTE_POLICY`; every route tested for every role; admin sees names only on duplicate review (recorded exception) | low; the admin exception is an owner decision |
| The model invents an answer | answer-fidelity gate `eval_chat.py` at 1.0; answers built only from the patient's own rows | medium - a model can still err |
| Health data leaves the machine | model, embeddings and index are local; voice demo fenced off and refused behind the tunnel | Cloudflare still sees portal traffic - DPA BLOCKED |
| Data kept after it should be gone | erasure across every store with tombstones re-applied on restore; retention sweep | retention periods are placeholders |
| Audit trail rewritten | append-only triggers; purges audited | someone with file access can still change the database |
| Lost or stolen laptop | FileVault on (checked 2026-09-22); backups encrypted | the live database has no encryption of its own |
| Backups lost with the machine | encrypted archives | **no off-machine copy - BLOCKED on D08** |

## Outcome

**BLOCKED.** Not to be signed off for real data until the controller, the legal basis, the
Cloudflare agreement and the off-machine backup destination are settled.
