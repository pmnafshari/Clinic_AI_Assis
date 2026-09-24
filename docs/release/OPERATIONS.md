# Operations

Everything here runs on the clinic's own Mac. Nothing is scheduled and nothing is sent anywhere:
each command is run by hand, or by a job the owner decides to install.

## Start and stop

```bash
ollama serve                                               # local model, loopback only
.venv/bin/python run.py                                    # staff app, 5000
PATIENT_COOKIE_SECURE=0 .venv/bin/python patient_run.py    # patient app, 5001 (drop the variable behind TLS)
.venv/bin/python site_run.py                               # public site, 5002
```

The staff and patient apps refuse to start when FileVault is not on (`disk_guard.py`); the public site holds no patient data and does not check.

## Health

```bash
.venv/bin/python health.py          # exit 1 and a line in db/alerts.log on any alert
.venv/bin/python health.py --json
```

Checks the three apps and the model (reachable, latency over 2 s), free disk (under 2 GiB), FileVault,
the newest backup (older than 26 h), the upload inbox (a file older than 15 min), reminder jobs stuck
or failed, unreconciled document temp files, provider switches and spend caps, deliveries with an
unknown outcome. People's queues (notes and documents awaiting a dentist, open call-backs) are
reported, not alerted. Output is counts and ages only - no names, codici fiscali or file names.

Not measured: model quality drift between gate runs (the evals are run by hand), provider latency
(no provider), load (see below). **Alerts reach nobody**: no recipient is approved yet.

## Backups and the restore drill

```bash
.venv/bin/python backup.py create --keep 14
.venv/bin/python backup.py verify --archive backups/clinic-<stamp>.cbk
.venv/bin/python restore_drill.py --rollback-ref <previous release commit> --out drill.json
```

See `docs/backup-runbook.md` for keys and restores. The drill takes a fresh backup, restores it into
a temporary folder, re-applies recorded erasures, rebuilds the search index, checks that a patient
erased after the backup stays erased, measures RPO (age of the newest backup when it started) and
RTO (restore wall time), and opens the restored database with the previous release's code. A backup
on this disk is not disaster recovery; an off-machine copy is an owner decision.

## CI

```bash
./ci.sh              # pins, pip check, fast suite, migrations, secret scan, dependency audit
./ci.sh --full       # plus the browser/model gate and the load check (services must be running)
./ci.sh --offline    # without the dependency audit (it sends package names to PyPI/OSV)
```

`ci_accepted_vulns.txt` lists the advisories that are reviewed and do not apply; any other fails.
`.secrets.baseline` lists reviewed false positives; any new finding fails. Hosted CI is not enabled.

## Load

```bash
.venv/bin/python load_check.py --users 20 --rounds 10
```

Runs against a disposable instance (temporary database on port 5003), never the real one. The
default is a demo volume; the clinic's real volume is not known yet.

## Rollback plan

1. Code: every release is one commit; `git revert <release commit>` and restart the apps. The drill's
   rollback rehearsal proves the previous commit opens the current database.
2. Data: stop the apps, `backup.py restore --archive <archive> --into <new dir> --apply --rebuild-index`,
   check it, then swap it in by hand. Restores never write into the live folder.
3. Anything that changed the schema says so in its release notes, with its own rollback.

## Deployment checklist (open)

TLS in front of the patient app, firewall rules, the tunnel or network path, access to the machine,
capacity at the real volume, microphone and hardware in the treatment room: none of these can be
checked until the owner names the environment.
