#!/bin/sh
set -e
for f in *_selftest.py ask.py agent.py upload_worker.py auth.py sort_files.py user_admin.py web_session.py storage.py watcher.py patient_auth.py extract_note.py patient_accessor.py chroma_scope_selftest.py tunnel_guard.py disk_guard.py validate_dataset.py; do .venv/bin/python "$f" --selftest; done
.venv/bin/python -m patient_app.net --selftest
.venv/bin/python -m patient_app.render --selftest
.venv/bin/python -m patient_app.chat --selftest
