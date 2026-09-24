# Architecture and data map

Three Flask apps on one Mac, one SQLite database, local files and a local search index. A small model
runs in Ollama on the same machine. Routes and tables: `REFERENCE.md` (generated).

| Component | Runs as | Reads/writes |
|---|---|---|
| Staff app `app/` | `run.py`, port 5000, loopback | database, `sorted/`, `drop/`, `staging/`, `documents/`, search index |
| Patient app `patient_app/` | `patient_run.py`, port 5001 | database (own patient only), its own session key `.env.patient` |
| Public site `site_app/` | `site_run.py`, port 5002 | `site_app/clinic.yaml` only; no patient data |
| Model | `ollama serve`, port 11434, loopback | nothing stored; called only from this machine |
| Workers | `watcher.py`, `upload_worker.py`, `document_worker.py` (sandboxed), jobs | inbox to sorted notes; document reading; reminders; stock |

## Where data lives
| Data | Location | Backed up | Leaves the machine |
|---|---|---|---|
| patients, visits, invoices, appointments, audit | `db/clinic.sqlite` | yes | no |
| filed notes and media | `sorted/<codice fiscale>/` | yes | no |
| uploads waiting for review | `staging/`, `drop/` | yes | no |
| patient documents | `documents/` | yes | no |
| search index | `db/chroma`, `db/doc_chroma` | rebuilt from the files on restore | no |
| erasure tombstones | `db/erasures.jsonl` | outside the backup on purpose | no |
| backup and erasure keys | `~/.clinic-backup.key`, `~/.clinic-erasure.key` | owner keeps a copy | no |
| alerts | `db/alerts.log` (counts only) | no | no |

Nothing clinical is sent to any outside service. The only network calls are the model on loopback,
and the dependency audit in `ci.sh`, which sends package names (never data) to PyPI/OSV.
Privacy records (data flow, DPIA, retention, records of processing, keys, incidents): `docs/privacy/`.
