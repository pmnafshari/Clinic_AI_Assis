# Incident runbook

> Demo template - see [README](README.md). The notification duties and deadlines belong to the
> controller and their adviser; the steps below are what the system lets you do.

## 1. Contain

- **Stolen credentials (staff):** disable the account on `/admin/users`. Its sessions end at once.
- **Stolen credentials (patient):** revoke the patient's PIN on their record. Their sessions end at once.
- **Portal exposed when it should not be:** stop the tunnel (`cloudflared`), then the patient app.
- **Suspected tampering:** stop all three apps before touching the data, so nothing overwrites evidence.

## 2. Find out what happened

- Audit trail: `sqlite3 db/clinic.sqlite "SELECT * FROM audit_log WHERE ts >= '<start>' ORDER BY id"`.
  Refusals are `allowed = 0`. Patients appear by surrogate id; `patients.patient_id` maps it back.
- Portal probing: `action = 'patient_scope_violation'` rows, with the source address when the
  tunnel header is trusted.
- Purges of the trail: `action = 'audit_purge'` rows say who removed how many rows, and why.

## 3. Recover

- Restore into a new directory, never over live data:
  `.venv/bin/python backup.py restore --archive backups/<file>.cbk --into /tmp/restore --apply --rebuild-index`.
  Recorded erasures are applied to the restored copy automatically.
- A restored archive that predates the surrogate id reports that erasures cannot be applied yet:
  run `migrate_pid.py` on it, then `python erasure.py reapply --root /tmp/restore`.

## 4. Record and notify

- Write down what happened, when it was found, what data and how many patients, what was done.
- The controller decides on notifying the supervisory authority and the patients, within the
  deadline the law sets. **BLOCKED** until there is a real controller.

## 5. Afterwards

- Rotate any key that may have been exposed (keys-and-encryption.md).
- Run the security scans again: `pip-audit` and `detect-secrets scan`.
- Add a test for whatever let the incident happen.
