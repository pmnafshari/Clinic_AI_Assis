# Keys and encryption

> Demo template - see [README](README.md).

## What is encrypted

| Data | At rest | In transit |
|---|---|---|
| Live database, `sorted/`, index | full-disk encryption only (FileVault, **On** at 2026-09-22); the apps refuse to start if it is off (`disk_guard.py`) | all three apps bind 127.0.0.1; the portal reaches the internet only through the HTTPS tunnel, with `Secure` cookies |
| Backups `backups/*.cbk` | AES-256-CBC with PBKDF2, HMAC-SHA256 over the ciphertext | not sent anywhere yet |
| Exports `exports/` | not encrypted; mode 600, deleted after 24 hours | handed over by download to an authenticated patient or dentist |
| Erasure tombstones `db/erasures.jsonl` | hold no codice fiscale - an HMAC under a separate key | - |

The live database has no encryption of its own. SQLCipher would add it, but the free build is not
in the stack; this is recorded as a gap, and real use stays closed until a destination and a
method are approved.

## Keys

| Key | File | Used for |
|---|---|---|
| Backup key | `~/.clinic-backup.key` (or `CLINIC_BACKUP_KEY_FILE`), mode 600 | encrypting and authenticating backups |
| Erasure key | `~/.clinic-erasure.key` (or `CLINIC_ERASURE_KEY_FILE`), mode 600, created 2026-09-22, fingerprint `23a83f4ed17d2cd3` | matching a restored patient to a tombstone without storing their code |
| App secrets | `.env`, `.env.patient`, gitignored | signing cookies |

Both keys live outside the repository and outside the backup set. Lose the backup key and every
backup is unreadable. Lose the erasure key and a restore can still match tombstones by surrogate
id, but not a patient who was re-created under a new id.

**Offline copies: BLOCKED.** Neither key has a copy off this machine: no approved encrypted
removable destination exists yet. A second copy on the same disk is not a backup and was not made.
The owner copies both keys to approved encrypted removable media kept apart from the machine.

A fingerprint is the first 16 hex characters of `shasum -a 256 <key file>`. It identifies a key
without revealing it.

## Rotating the backup key

```bash
.venv/bin/python backup.py init-key --key-file ~/.clinic-backup.key.new
.venv/bin/python backup.py rekey --new-key-file ~/.clinic-backup.key.new
# every archive is now under the new key and verified with it
mv ~/.clinic-backup.key ~/.clinic-backup.key.old
mv ~/.clinic-backup.key.new ~/.clinic-backup.key
.venv/bin/python backup.py verify --archive backups/<newest>.cbk
# then destroy ~/.clinic-backup.key.old
```

`rekey` never writes plaintext to disk and verifies each archive under the new key before it
replaces the old one. If it stops half way, the archives it did not reach are still under the old
key - run it again with the same two keys.

## Migration safety copies

A migration takes an encrypted archive first (`migrate_audit.py` does this through
`backup.create`). The Phase 52 conversion left a plaintext copy,
`backups/pre-p52-tz-20260916T213905Z.sqlite`. Its encrypted copy,
`backups/migration/clinic-pre-p52-tz-20260916T213905Z.cbk`, was restored into a scratch
directory on 2026-09-22 and matched it exactly: same 23 schema objects, same rows in all 18
tables, integrity ok. The plaintext file was then moved to the Trash (recoverable, not deleted).
No plaintext database copy is left in `backups/`.
