# Runbooks

Each runbook: what normal looks like, what to run, and when to stop and ask. Commands run from the
repository folder with the virtual environment.

## Payments
Normal: payments are recorded by staff at the desk (cash, card terminal, transfer) on the invoice
page. There is no online payment provider, so a "pay link" is refused on purpose.
- Wrong amount recorded: use **Refund** or **Reverse** on the payment - never edit or delete it.
- A bank transfer that does not match: **Reconcile** on the invoice, with the outcome.
- Installments: set on an issued invoice; each installment's due date drives a reminder.
- Stop and ask the owner before any change to how money is counted or shown.

## Reminders
Normal: `reminder_job.py` plans reminders for appointments, issued invoices and installments. With no
messaging provider every due reminder is **held**, not sent - the reminders page says so.
```bash
.venv/bin/python reminder_job.py        # plan the queue, release stale claims, report; sends nothing
```
- `health.py` alerts on reminder jobs stuck in a claim or failed; stuck claims are released
  automatically after their timeout, failed ones can be retried from the reminders page.
- Quiet hours and the money rules are policy; do not change them without the owner.

## Inventory
```bash
.venv/bin/python inventory_job.py       # the low-stock check
```
Corrections are movements (in, out, adjustment) with a reason; the history is never edited.

## Documents
Uploaded documents are read in a sandbox and wait for a dentist. After a crash or power cut:
```bash
.venv/bin/python documents.py --reconcile           # report only
.venv/bin/python documents.py --reconcile --apply   # remove orphan files and stale temps; rows are never touched
```
A missing or changed original is reported, not repaired: restore it from a backup.

## Sessions and retention
```bash
.venv/bin/python retention.py            # dry run: what the retention rules would remove
.venv/bin/python retention.py --apply    # after a verified backup
.venv/bin/python unlock_user.py <username>
```

## Outage
| Symptom | Check | Action |
|---|---|---|
| a page does not load | `health.py` | restart that app (see OPERATIONS.md); if it will not start, read its refusal - FileVault, codice fiscale and timezone guards stop it on purpose |
| the assistant or extraction fails | `health.py` shows `app.model` down | `ollama serve`; notes keep arriving in the inbox and wait |
| disk nearly full | `health.py disk.free_gib` | free space before anything else; backups refuse to start without room |
| the database is damaged | `sqlite3 db/clinic.sqlite 'PRAGMA integrity_check'` | stop the apps, restore the newest verified backup into a new folder (OPERATIONS.md), check, swap |
| the phone line | - | there is no live phone line in this release |
Report every outage that touched patient data through `docs/privacy/incident-runbook.md`.
