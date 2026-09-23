# Data flow

> Demo template - see [README](README.md).

## Where data comes from and where it goes

```
staff browser ──(loopback only)──► staff app :5000 ──► db/clinic.sqlite
    │                                   │                sorted/<patient id>/
    │ upload                            │                db/chroma (search index)
    ▼                                   ▼                db/undo_log.jsonl
  drop/ ──► upload worker ──► local model (ollama) ──► sorted/ + sqlite + index

patient browser ──(https tunnel)──► patient app :5001 ──► same sqlite, read through
                                         │               patient_accessor (own rows only)
                                         └──► local model (ollama) for the assistant,
                                              only with ai_assistant consent

public site :5002 ──(VOICE_DEMO=1 only)──► Deepgram, ElevenLabs   [demo, off by default]
```

## Inventory

| Data | Stored in | Processed by | Leaves the machine? |
|---|---|---|---|
| Name, codice fiscale, phone | `patients` | staff app, patient app | no |
| Visits, procedures, clinical notes | `visits`, `sorted/<id>/notes` | staff app, local model, search index | no |
| Invoices | `invoices`, `sorted/<id>/records` | staff app, patient app | no |
| Appointments | `appointments` | staff app, patient app | no |
| Invoice status, payments, installments | `billing_invoices`, `payments`, `payment_allocations`, `installments` | staff app (dentist changes, assistant views), patient app (own only) | no - payments are typed in by staff; no provider, no money moves |
| Consent records | `consent_records` | staff app, patient app | no |
| Data requests and exports | `data_requests`, `exports/` (24 h) | staff app, patient app | only when handed to the patient |
| Audit trail | `audit_log` (surrogate ids only) | all apps | no |
| Patient documents (PDF, images, text) | `documents/<patient id>/<sha256>` (0600), `patient_documents`, `db/doc_chroma` after a dentist confirms | staff app (dentist only), sandboxed local reader (pypdf, Tesseract), local embedder | no - the reader runs with the network denied by the OS |
| Dictation audio | never stored | local speech model (faster-whisper, off by default) | no |
| Portal traffic | - | Cloudflare tunnel (TLS termination) | **yes - Cloudflare is a processor** |
| Voice demo audio | - | Deepgram, ElevenLabs | **yes - demo only, off by default, refused behind the tunnel; public-site visitors, never a patient session** |
| Backups | `backups/*.cbk`, encrypted | `backup.py` | no (off-machine destination BLOCKED on D08) |
| Training data | synthetic only | Google Colab, a cloud model as dataset teacher | yes, synthetic only - never real records |

## Processors

| Processor | Role | Agreement |
|---|---|---|
| Cloudflare | tunnel in front of the patient portal | **BLOCKED** - DPA not signed (GDPR-01) |
| Deepgram, ElevenLabs | voice demo speech-to-text and speech | **none - must never receive real patient audio** |
| Google (Colab) | model training on synthetic data | not needed while only synthetic data is used |

Nothing sends a message or records a call today. Consent for messaging and call recording is
recorded and withdrawable now so the reminder engine (P09) and voice scheduling (P12) find it in
place; each must check it at the moment it acts.

The model, the embeddings and the search index run on the clinic's own machine. Fonts and icons
are served locally so no third party sees a visitor.
