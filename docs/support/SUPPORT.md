# Support guide (P21)

Status: **setup done, programme ACTIVE**. Support is never "finished". This guide is for the human owner of
the service; the owner, the contact path and the service levels are **TBC** until the clinic names them.

| | |
|---|---|
| Service owner | TBC (owner to name) |
| Contact path for staff | TBC (owner to name); until then, tell the dentist on duty |
| Service levels (SLA/SLO) | none approved - there is no error budget yet |
| Alert recipient | none approved (D11) - alerts stay in `db/alerts.log` and become tickets |
| Schedule | templates only (below), **not installed** |

## Every day (or when something looks wrong)
1. `.venv/bin/python health.py` - exit 1 means at least one alert; each alert is also a line in `db/alerts.log`.
2. `.venv/bin/python support.py from-alerts --since <last time you did this, ISO UTC>` - one ticket per new alert.
3. `.venv/bin/python support.py list` - triage each open ticket (who, by when), fix it, close it with what you did.
   Tickets are about the system: a patient's name, codice fiscale, phone or e-mail is refused.
4. Anything touching patient data: `docs/privacy/incident-runbook.md`. X-ray demo incidents:
   `docs/release/RUNBOOKS.md`.

## Every period (weekly in the first months, then as the owner decides)
1. `.venv/bin/python period_run.py run` - health sample, backup + verify, restore drill, fixed eval. A dropped
   metric, a failed drill or a failed backup opens a ticket that says what to do.
2. `.venv/bin/python period_run.py report --matrix ../new_steps_approach.md` - availability, tickets, restore
   age, eval metrics, cost, progress per phase. Keep the report with the period.
3. Review with the owner: language/shorthand accuracy of the notes model (`eval_notes.py`, needs the model),
   chat answer fidelity (`eval_chat.py`), retrieval, any clinical error reported, drift, provider cost (zero
   while no provider is connected). Decide the next review date.

## Maintenance (P21.04)
- **Dependency update:** change one pin in `requirements*.txt` -> `.venv/bin/pip install -r requirements.txt`
  -> `./ci.sh --full` (the gate runs on disposable instances) -> `restore_drill.py` -> commit. A new advisory
  fails `ci.sh` until it is fixed or reviewed into `ci_accepted_vulns.txt` with a reason.
- **Model update:** never without the fixed evals (`eval_notes.py`, `eval_chat.py`, `verify_retrain.py`) and,
  for anything clinical, the clinical owner's approval.
- **Consent, retention, processors, licences:** re-read `docs/privacy/` and `THIRD_PARTY_NOTICES.md` each period;
  changes need the owner (and legal where marked).

## Onboarding a new staff member
Create the account (admin), give them `docs/release/GUIDES.md` for their role, walk the relevant part of the human
review package with them, and record who trained whom in the period notes.

## Schedule templates (not installed)
`launchd/clinic.health.plist.example` (hourly health + tickets) and `launchd/clinic.period.plist.example` (weekly
period run). Installing one is the owner's decision: copy it to `~/Library/LaunchAgents/`, fix the paths, then
`launchctl load <file>`. Until then nothing runs on its own, and nothing here claims it does.
