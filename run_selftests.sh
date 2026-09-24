#!/bin/sh
set -e
for f in *_selftest.py ask.py agent.py upload_worker.py auth.py sort_files.py user_admin.py web_session.py storage.py watcher.py patient_auth.py extract_note.py patient_accessor.py chroma_scope_selftest.py tunnel_guard.py disk_guard.py validate_dataset.py voice_config.py voice.py appointments.py shared/names.py backup.py codice_fiscale.py patient_identity.py clinic_time.py migrate_tz.py availability.py patient_id.py demo_fixtures.py action_log.py migrate_audit.py consent.py data_rights.py erasure.py retention.py ledger.py migrate_ledger.py demo_billing.py inventory.py inventory_job.py demo_inventory.py reminders.py reminder_job.py handoff.py patient_faq.py patient_agent.py providers.py payments.py delivery.py calls.py health.py restore_drill.py release_docs.py load_check.py xray_gate.py migrate_p22.py; do .venv/bin/python "$f" --selftest; done
.venv/bin/python -m patient_app.net --selftest
.venv/bin/python -m patient_app.render --selftest
.venv/bin/python -m patient_app.chat --selftest
# P10 T5: deterministic, no server and no model, so it belongs in the fast suite
.venv/bin/python eval_agent.py
