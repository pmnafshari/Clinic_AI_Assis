# Backup and restore runbook

`backup.py` makes encrypted, verifiable backups of the clinic's data, and restores them into a new, empty directory. It never restores over the live data.

## What is covered

| Store | How it is backed up |
|---|---|
| `db/clinic.sqlite` | copied with SQLite's online backup API, so it is consistent while the apps run |
| `sorted/`, `drop/`, `db/undo_log.jsonl` | copied as files, after the database snapshot |
| `db/chroma` (search index) | **not copied**. It is derived data, rebuilt from `sorted/` on restore (`--rebuild-index`) |
| `log.txt` | not backed up. It is operational output, not clinic data |

## One-time setup (owner)

1. Create the key:

   ```bash
   .venv/bin/python backup.py init-key
   ```

   This writes `~/.clinic-backup.key` with mode 600.
2. **Keep a second copy of that key somewhere safe and offline.** Without it, every backup is unreadable. There is no recovery.
3. Choose an off-machine destination: an encrypted external disk, or an approved EU storage provider.
   - A backup on the same disk as the data is a local snapshot, **not** disaster recovery.
   - This choice is open decision D08.

## Make a backup

```bash
.venv/bin/python backup.py create --dest /Volumes/ClinicBackup --keep 14
```

- It refuses when there is not enough free disk, or when the key is missing or readable by other users.
- A failed run leaves no file behind. The final `.cbk` only appears after the archive is complete and authenticated.

## Check a backup

```bash
.venv/bin/python backup.py verify --archive /Volumes/ClinicBackup/clinic-20260910T225510Z.cbk
```

This decrypts to a private temp dir and checks every file checksum, SQLite `integrity_check`, and every table's row count against the manifest.

## Restore

1. **Dry run** (the default). It writes nothing:

   ```bash
   .venv/bin/python backup.py restore --archive X.cbk --into /tmp/restore-check
   ```

2. **Apply** into a new, empty directory, and rebuild the search index:

   ```bash
   .venv/bin/python backup.py restore --archive X.cbk --into /srv/clinic-restore --apply --rebuild-index
   ```

3. Stop the apps. Move the current `db/`, `sorted/` and `drop/` aside (do not delete them). Move the restored ones into place, then start the apps.
   - `run.py` and `patient_run.py` refuse to start without FileVault.
   - A restore into the live directory, or into any non-empty directory, is refused.

**Known limit until P06:** a patient deletion approved after a backup was taken would come back on restore. Deletions must be re-applied after any restore, and there is no tombstone mechanism yet.

## Scheduling (not active until the owner loads it)

Save this as `~/Library/LaunchAgents/clinic.backup.plist`, adjust the paths, then run `launchctl load` on it. It runs daily at 02:30.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>clinic.backup</string>
  <key>WorkingDirectory</key><string>/path/to/Demo</string>
  <key>ProgramArguments</key><array>
    <string>/path/to/Demo/.venv/bin/python</string><string>backup.py</string>
    <string>create</string><string>--dest</string><string>/Volumes/ClinicBackup</string>
    <string>--keep</string><string>14</string>
  </array>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>2</integer><key>Minute</key><integer>30</integer></dict>
  <key>StandardErrorPath</key><string>/path/to/Demo/backups/backup-errors.log</string>
</dict></plist>
```

Nothing alerts on a failed or stale backup yet; that is P17/P21. Until then, check that the newest `.cbk` is less than a day old.

## Drill record

| Date (UTC) | Commit | Data | Backup | Restore + rebuild | Result |
|---|---|---|---|---|---|
| 2026-09-10T22:55Z | 9c7e7c2 + P01 working tree | dev stores, synthetic | 0.17 s, 60 KB, 57 files | 3 s, 25 notes re-indexed | all checksums and row counts equal |
