#!/bin/sh
set -e
for f in *_selftest.py ask.py agent.py upload_worker.py auth.py sort_files.py user_admin.py web_session.py storage.py watcher.py patient_auth.py extract_note.py; do .venv/bin/python "$f" --selftest; done
