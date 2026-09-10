"""encrypted, verifiable backups of the clinic's data, and a restore that never
touches the live stores.

what a backup holds:
  db/clinic.sqlite    copied with sqlite's online backup api, so a write in
                      flight cannot tear it - a plain file copy of a live db can
  db/undo_log.jsonl   the before-images the agent's undo reads
  sorted/, drop/      filed notes and media, and the inbound queue
  db/chroma           NEVER copied. chroma keeps binary index segments beside
                      its own sqlite and does not hold them open between writes,
                      so "is anyone using it" cannot be detected - lsof reported
                      a collection this very process had open as unused. a copy
                      taken mid-write is an index that disagrees with itself. the
                      index is derived data: restore regenerates it from sorted/
                      with the app's own sync (--rebuild-index), and the
                      selftest proves a search works afterwards.

the sqlite snapshot is taken BEFORE the files are read. files only grow, so a
restored db never points at a note that is not in the archive; a note filed
mid-backup lands as an orphan file, and the rebuild syncs it.

format: tar.gz, encrypted with the openssl cli (aes-256-cbc, pbkdf2), then an
hmac-sha256 over the ciphertext appended as the last 32 bytes - encrypt-then-
mac, so a wrong key and a flipped byte are both refused before anything is
decrypted. the plaintext never touches disk except the sqlite staging copy,
which lives in a 0700 temp dir inside the destination and is removed in a
finally. the manifest (file names carry codici fiscali) is inside the
encryption, never beside it.

the key is a file outside the repo (~/.clinic-backup.key, or
CLINIC_BACKUP_KEY_FILE), mode 600. lose it and every backup is unreadable -
keeping a second copy somewhere safe is the owner's job, not this script's.

    .venv/bin/python backup.py init-key
    .venv/bin/python backup.py create [--dest backups] [--keep 14]
    .venv/bin/python backup.py verify --archive backups/clinic-....cbk
    .venv/bin/python backup.py restore --archive X --into /tmp/restore   # dry run
    .venv/bin/python backup.py restore --archive X --into /tmp/restore --apply [--rebuild-index]
    .venv/bin/python backup.py --selftest

a backup on the same disk as the data is a local snapshot, not disaster
recovery. an off-machine destination is an owner decision (D08).
"""

import hashlib
import hmac
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_KEY_FILE = Path.home() / ".clinic-backup.key"
KEY_ENV = "CLINIC_BACKUP_KEY_FILE"
PBKDF2_ITER = "600000"
MAC_LEN = 32
NAME_PREFIX = "clinic-"
SUFFIX = ".cbk"
# stores, relative to the data root. the sqlite db is handled separately.
FILE_STORES = ("sorted", "drop", "db/undo_log.jsonl")
DB_REL = "db/clinic.sqlite"
CHROMA_REL = "db/chroma"


class BackupError(Exception):
    pass


# --- key ------------------------------------------------------------------


def key_file(path=None):
    return Path(path or os.environ.get(KEY_ENV) or DEFAULT_KEY_FILE)


def read_key(path=None):
    path = key_file(path)
    if not path.exists():
        raise BackupError(f"no key file at {path}. create one with: backup.py init-key")
    # a key anyone else on the machine can read protects nothing
    if path.stat().st_mode & 0o077:
        raise BackupError(f"{path} is readable by other users - chmod 600 it")
    key = path.read_bytes().strip()
    if len(key) < 32:
        raise BackupError(f"{path} holds too short a key")
    return path, key


def init_key(path=None):
    path = key_file(path)
    if path.exists():
        raise BackupError(f"{path} already exists - refusing to overwrite a key backups may depend on")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(os.urandom(32).hex() + "\n")
    return path


def mac_key(key):
    # a separate key for the mac, derived from the same secret, so the
    # encryption key is never also used to authenticate
    return hashlib.pbkdf2_hmac("sha256", key, b"clinic-backup-mac", 200_000)


def openssl():
    exe = os.environ.get("OPENSSL") or shutil.which("openssl")
    if not exe:
        raise BackupError("openssl not found on PATH")
    return exe


# --- create ---------------------------------------------------------------


def free_bytes(path):
    return shutil.disk_usage(path).free


def tree_size(path):
    if path.is_file():
        return path.stat().st_size
    if not path.exists():
        return 0
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def snapshot_db(live_db, staged_db):
    src = sqlite3.connect(f"file:{live_db}?mode=ro", uri=True)
    dst = sqlite3.connect(staged_db)
    try:
        src.backup(dst)
    finally:
        src.close()
    check = dst.execute("PRAGMA integrity_check").fetchone()[0]
    if check != "ok":
        dst.close()
        raise BackupError(f"sqlite snapshot failed its integrity check: {check}")
    tables = [r[0] for r in dst.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    counts = {t: dst.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}
    user_version = dst.execute("PRAGMA user_version").fetchone()[0]
    dst.close()
    return counts, user_version


def git_commit():
    result = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True)
    return result.stdout.strip() or None


def create(data_root=ROOT, dest=None, key_path=None, keep=None, stamp=None):
    data_root = Path(data_root)
    dest = Path(dest or data_root / "backups")
    live_db = data_root / DB_REL
    if not live_db.exists():
        raise BackupError(f"no database at {live_db}")
    key_path, key = read_key(key_path)
    dest.mkdir(parents=True, exist_ok=True)

    stores = [data_root / s for s in FILE_STORES] + [live_db]
    # gzip shrinks this, but the staging copy of the db is full size and a
    # nearly full disk is the normal state of this machine - so ask for twice
    # the raw size plus headroom, and refuse before writing anything
    needed = 2 * sum(tree_size(p) for p in stores) + 50 * 1024 * 1024
    if free_bytes(dest) < needed:
        raise BackupError(f"not enough free space in {dest}: need {needed // (1 << 20)} MiB, "
                          f"have {free_bytes(dest) // (1 << 20)} MiB")

    stamp = stamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    final = dest / f"{NAME_PREFIX}{stamp}{SUFFIX}"
    if final.exists():
        raise BackupError(f"{final} already exists")
    partial = final.with_suffix(SUFFIX + ".partial")
    stage = Path(tempfile.mkdtemp(prefix=".stage-", dir=dest))
    os.chmod(stage, 0o700)
    started = time.monotonic()
    try:
        staged_db = stage / "clinic.sqlite"
        counts, user_version = snapshot_db(live_db, staged_db)

        members = [(staged_db, DB_REL)]
        for rel in FILE_STORES:
            path = data_root / rel
            if path.is_file():
                members.append((path, rel))
            elif path.is_dir():
                for f in sorted(path.rglob("*")):
                    if f.is_file():
                        members.append((f, str(f.relative_to(data_root))))

        manifest = {
            "format": 1,
            "created_utc": stamp,
            "git_commit": git_commit(),
            "sqlite_user_version": user_version,
            "table_counts": counts,
            "chroma": "rebuild-from-sorted",
            "files": {rel: {"sha256": sha256_file(src), "bytes": src.stat().st_size} for src, rel in members},
        }

        with open(partial, "wb") as out:
            proc = subprocess.Popen(
                [openssl(), "enc", "-aes-256-cbc", "-pbkdf2", "-iter", PBKDF2_ITER, "-salt",
                 "-pass", f"file:{key_path}"],
                stdin=subprocess.PIPE, stdout=out)
            with tarfile.open(fileobj=proc.stdin, mode="w|gz") as tar:
                data = json.dumps(manifest, indent=2, sort_keys=True).encode()
                info = tarfile.TarInfo("manifest.json")
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
                for src, rel in members:
                    tar.add(src, arcname=rel, recursive=False)
            proc.stdin.close()
            if proc.wait() != 0:
                raise BackupError("openssl encryption failed")

        tag = hmac.new(mac_key(key), digestmod=hashlib.sha256)
        with open(partial, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                tag.update(block)
        with open(partial, "ab") as f:
            f.write(tag.digest())
            f.flush()
            os.fsync(f.fileno())
        # the rename is the commit point - until it happens there is no file
        # anyone could mistake for a finished backup
        os.replace(partial, final)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        if partial.exists():
            partial.unlink()

    removed = rotate(dest, keep) if keep else []
    return {"archive": str(final), "seconds": round(time.monotonic() - started, 2),
            "bytes": final.stat().st_size, "chroma": manifest["chroma"],
            "table_counts": counts, "files": len(members), "rotated_out": removed}


def rotate(dest, keep):
    archives = sorted(Path(dest).glob(f"{NAME_PREFIX}*{SUFFIX}"))
    removed = []
    for old in archives[:-keep] if keep < len(archives) else []:
        old.unlink()
        removed.append(old.name)
    return removed


# --- verify and restore -----------------------------------------------------


def check_mac(archive, key):
    size = archive.stat().st_size
    if size <= MAC_LEN:
        raise BackupError(f"{archive} is too short to be a backup")
    tag = hmac.new(mac_key(key), digestmod=hashlib.sha256)
    remaining = size - MAC_LEN
    with open(archive, "rb") as f:
        while remaining:
            block = f.read(min(1 << 20, remaining))
            tag.update(block)
            remaining -= len(block)
        stored = f.read(MAC_LEN)
    if not hmac.compare_digest(tag.digest(), stored):
        raise BackupError("archive does not verify: wrong key, or the file is corrupted or truncated")
    return size - MAC_LEN


def extract(archive, key_path, key, into):
    cipher_len = check_mac(archive, key)
    proc = subprocess.Popen(
        [openssl(), "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", PBKDF2_ITER,
         "-pass", f"file:{key_path}"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE)

    def feed():
        with open(archive, "rb") as f:
            remaining = cipher_len
            while remaining:
                block = f.read(min(1 << 20, remaining))
                proc.stdin.write(block)
                remaining -= len(block)
        proc.stdin.close()

    import threading
    # daemon: if extraction ever raised, a feeder blocked on a full pipe must
    # not keep the process alive
    writer = threading.Thread(target=feed, daemon=True)
    writer.start()
    # the "data" filter refuses absolute paths, .. and links out of the tree
    with tarfile.open(fileobj=proc.stdout, mode="r|gz") as tar:
        tar.extractall(into, filter="data")
    writer.join()
    if proc.wait() != 0:
        raise BackupError("openssl decryption failed")
    return json.loads((Path(into) / "manifest.json").read_text())


def check_restored(root, manifest):
    root = Path(root)
    problems = []
    for rel, meta in manifest["files"].items():
        path = root / rel
        if not path.is_file():
            problems.append(f"missing {rel}")
        elif sha256_file(path) != meta["sha256"]:
            problems.append(f"checksum mismatch {rel}")
    db = sqlite3.connect(root / DB_REL)
    try:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            problems.append("restored db fails integrity_check")
        for table, expected in manifest["table_counts"].items():
            got = db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            if got != expected:
                problems.append(f"{table}: {got} rows, manifest says {expected}")
    finally:
        db.close()
    return problems


def verify(archive, key_path=None):
    key_path, key = read_key(key_path)
    with tempfile.TemporaryDirectory() as tmp:
        os.chmod(tmp, 0o700)
        manifest = extract(Path(archive), key_path, key, tmp)
        problems = check_restored(tmp, manifest)
    return manifest, problems


def restore(archive, into, key_path=None, apply=False, rebuild_index=False):
    into = Path(into)
    # never into the live data, and never over something that is already
    # there - a restore that silently merges into an existing tree is how a
    # good backup destroys good data
    if into.resolve() in (ROOT.resolve(), (ROOT / "db").resolve()):
        raise BackupError("refusing to restore into the live data directory")
    if into.exists() and any(into.iterdir()):
        raise BackupError(f"{into} is not empty - restore only into a new, empty directory")
    manifest, problems = verify(archive, key_path)
    if problems or not apply:
        return {"applied": False, "problems": problems, "manifest_counts": manifest["table_counts"],
                "chroma": manifest["chroma"]}
    key_path, key = read_key(key_path)
    into.mkdir(parents=True, exist_ok=True)
    extract(Path(archive), key_path, key, into)
    (into / "manifest.json").unlink()
    problems = check_restored(into, manifest)
    rebuilt = None
    if rebuild_index and not problems:
        rebuilt = rebuild(into)
    return {"applied": True, "problems": problems, "manifest_counts": manifest["table_counts"],
            "chroma": manifest["chroma"], "rebuilt": rebuilt}


def rebuild(root):
    # regenerates ONLY the vector index from the restored notes, through the
    # app's own chroma upsert. it deliberately does not call backfill_sorted:
    # that re-syncs sqlite too, and even for a note it reports as already
    # synced it deletes and re-inserts the invoice lines, so invoice ids move
    # (found building this, 2026-09-11). a restore must leave the sqlite it
    # just verified exactly as the backup had it.
    import storage
    from dental_notes_schema import DentalNote
    root = Path(root)
    collection = storage.get_collection(str(root / CHROMA_REL))
    indexed, failed = 0, []
    for json_path in sorted((root / "sorted").glob("*/notes/*.json")):
        try:
            note = DentalNote.model_validate_json(json_path.read_text())
            storage.upsert_note_chroma(note, str(json_path.relative_to(root / "sorted")), collection)
            indexed += 1
        except Exception:
            failed.append(str(json_path.relative_to(root)))
    return {"indexed": indexed, "failed": failed}


# --- cli ------------------------------------------------------------------


def arg(argv, name, default=None):
    if name in argv:
        return argv[argv.index(name) + 1]
    return default


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv[0] == "--selftest":
        selftest()
        return 0
    try:
        cmd = argv[0]
        if cmd == "init-key":
            print(f"key written to {init_key(arg(argv, '--key-file'))} (mode 600). keep a second copy somewhere safe.")
        elif cmd == "create":
            keep = arg(argv, "--keep")
            print(json.dumps(create(dest=arg(argv, "--dest"), key_path=arg(argv, "--key-file"),
                                    keep=int(keep) if keep else None), indent=2))
        elif cmd == "verify":
            manifest, problems = verify(arg(argv, "--archive"), arg(argv, "--key-file"))
            print(json.dumps({"created_utc": manifest["created_utc"], "chroma": manifest["chroma"],
                              "table_counts": manifest["table_counts"], "files": len(manifest["files"]),
                              "problems": problems}, indent=2))
            return 1 if problems else 0
        elif cmd == "restore":
            result = restore(arg(argv, "--archive"), arg(argv, "--into"), arg(argv, "--key-file"),
                             apply="--apply" in argv, rebuild_index="--rebuild-index" in argv)
            print(json.dumps(result, indent=2))
            return 1 if result["problems"] else 0
        else:
            print(f"unknown command {cmd!r}")
            return 2
    except BackupError as e:
        print(f"backup: {e}", file=sys.stderr)
        return 1
    return 0


# --- selftest -------------------------------------------------------------


def selftest():
    import threading

    import storage
    from dental_notes_schema import DentalNote

    tmp_root = Path(tempfile.mkdtemp(prefix="backup-selftest-"))
    try:
        data = tmp_root / "data"
        (data / "db").mkdir(parents=True)
        (data / "drop").mkdir()
        conn = storage.init_db(str(data / DB_REL))
        collection = storage.get_collection(str(data / CHROMA_REL))
        note = DentalNote(patient_name="Zzb Backuptest", codice_fiscale="ZZBK800101010101",
                          phone="3330001111", visit_date="2026-03-04", procedures=["rct 46"],
                          invoices=[{"amount": 120.5, "description": "rct 46"}],
                          clinical_notes="root canal on the lower left molar, zzb marker")
        storage.save_new_note(note, conn, collection, "dentist", "zzb_dentist", sorted_root=data / "sorted")
        (data / "sorted" / "ZZBK800101010101" / "xray.jpg").write_bytes(b"\xff\xd8zzb fake image")
        (data / "drop" / "incoming.txt").write_text("zzb queued note")
        (data / "db" / "undo_log.jsonl").write_text('{"username": "zzb"}\n')
        conn.close()

        key = tmp_root / "key"
        key.write_text(os.urandom(32).hex())
        os.chmod(key, 0o600)
        dest = tmp_root / "backups"

        # 1. T1 - backup, restore into a separate dir, everything equal
        made = create(data, dest, key, stamp="20260101T000000Z")
        archive = Path(made["archive"])
        assert archive.exists() and not list(dest.glob("*.partial")), "1: a finished archive and no partial"
        assert not list(dest.glob(".stage-*")), "1: the plaintext staging dir must be gone"
        assert b"ZZBK800101010101" not in archive.read_bytes(), "1: a codice fiscale must not be readable in the archive"
        # the live index is never copied - see the module docstring
        assert made["chroma"] == "rebuild-from-sorted", f"1: chroma must not be copied: {made['chroma']}"

        into = tmp_root / "restored"
        dry = restore(archive, into, key)
        assert dry["applied"] is False and not dry["problems"], f"1: dry run verifies clean: {dry}"
        assert not into.exists(), "1: a dry run writes nothing"

        done = restore(archive, into, key, apply=True, rebuild_index=True)
        staged = Path(tempfile.mkdtemp(dir=tmp_root))
        extract(archive, *read_key(key), staged)
        dry_db_sha = sha256_file(staged / DB_REL)
        assert done["applied"] and not done["problems"], f"1: restore applies clean: {done}"
        src = storage.connect(str(data / DB_REL))
        dst = storage.connect(str(into / DB_REL))
        for table in ("patients", "visits", "invoices"):
            a = src.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            b = dst.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            assert [tuple(r) for r in a] == [tuple(r) for r in b], f"1: {table} differs after restore"
        # the relation survived, not just the row counts
        joined = dst.execute("SELECT p.patient_name, i.amount FROM invoices i"
                             " JOIN visits v ON v.id = i.visit_id"
                             " JOIN patients p ON p.codice_fiscale = v.codice_fiscale").fetchall()
        assert [tuple(r) for r in joined] == [("Zzb Backuptest", 120.5)], f"1: invoice->visit->patient: {joined}"
        src.close()
        dst.close()
        for rel in ("sorted/ZZBK800101010101/xray.jpg", "drop/incoming.txt", "db/undo_log.jsonl"):
            assert sha256_file(data / rel) == sha256_file(into / rel), f"1: {rel} differs"
        # the rebuild touched only the index - the restored sqlite still has
        # the backup's exact bytes
        assert sha256_file(into / DB_REL) == dry_db_sha, "1: the index rebuild must not write to sqlite"
        assert done["rebuilt"] == {"indexed": 1, "failed": []}, f"1: rebuild: {done['rebuilt']}"
        # and the rebuilt index answers a search from the restored notes
        hits = storage.get_collection(str(into / CHROMA_REL)).query(
            query_texts=["root canal lower left molar"], n_results=1)
        assert hits["metadatas"][0][0]["codice_fiscale"] == "ZZBK800101010101", \
            f"1: the rebuilt index must find the restored note: {hits['metadatas']}"

        # 2. T2a - a writer running during the backup cannot tear the copy.
        # the restored db must pass integrity_check and hold exactly the rows
        # the manifest recorded at snapshot time
        stop = threading.Event()

        def writer():
            w = sqlite3.connect(data / DB_REL)
            while not stop.is_set():
                w.execute("INSERT INTO audit_log (ts, username, role, action, target, allowed)"
                          " VALUES ('t', 'zzb_writer', 'dentist', 'zzb_write', NULL, 1)")
                w.commit()
            w.close()

        t = threading.Thread(target=writer)
        t.start()
        try:
            busy = create(data, dest, key, stamp="20260101T000001Z")
        finally:
            stop.set()
            t.join()
        manifest, problems = verify(busy["archive"], key)
        assert not problems, f"2: a backup taken under writes must verify clean: {problems}"
        assert manifest["table_counts"]["audit_log"] == busy["table_counts"]["audit_log"], "2: counts agree"

        # 3. T2b - too little disk: refused before a single byte is written
        before = sorted(p.name for p in dest.iterdir())
        real_free = free_bytes
        globals()["free_bytes"] = lambda path: 1024
        try:
            create(data, dest, key, stamp="20260101T000002Z")
            raise AssertionError("3: a full disk must refuse the backup")
        except BackupError as e:
            assert "free space" in str(e), f"3: the refusal must say why: {e}"
        finally:
            globals()["free_bytes"] = real_free
        assert sorted(p.name for p in dest.iterdir()) == before, "3: nothing may be left behind"

        # 4. T2c - a flipped byte in the middle is refused, not half-restored
        corrupt = tmp_root / "corrupt.cbk"
        raw = bytearray(archive.read_bytes())
        raw[len(raw) // 2] ^= 0x01
        corrupt.write_bytes(bytes(raw))
        try:
            verify(corrupt, key)
            raise AssertionError("4: a corrupted archive must not verify")
        except BackupError as e:
            assert "does not verify" in str(e), f"4: {e}"
        truncated = tmp_root / "truncated.cbk"
        truncated.write_bytes(archive.read_bytes()[:-100])
        try:
            verify(truncated, key)
            raise AssertionError("4: a truncated archive must not verify")
        except BackupError:
            pass

        # 5. T2d - the wrong key is refused the same way
        other = tmp_root / "other-key"
        other.write_text(os.urandom(32).hex())
        os.chmod(other, 0o600)
        try:
            verify(archive, other)
            raise AssertionError("5: a wrong key must not verify")
        except BackupError as e:
            assert "does not verify" in str(e), f"5: {e}"

        # 6. a key other users can read is refused, and init-key never overwrites
        loose = tmp_root / "loose-key"
        loose.write_text(os.urandom(32).hex())
        os.chmod(loose, 0o644)
        try:
            read_key(loose)
            raise AssertionError("6: a world-readable key must be refused")
        except BackupError as e:
            assert "chmod 600" in str(e), f"6: {e}"
        try:
            init_key(key)
            raise AssertionError("6: init-key must not overwrite an existing key")
        except BackupError:
            pass

        # 7. T3 - restore refuses a non-empty target and the live data dir,
        # and leaves the archive untouched
        archive_sha = sha256_file(archive)
        try:
            restore(archive, into, key, apply=True)
            raise AssertionError("7: restore into a non-empty dir must be refused")
        except BackupError as e:
            assert "not empty" in str(e), f"7: {e}"
        try:
            restore(archive, ROOT / "db", key, apply=True)
            raise AssertionError("7: restore into the live db dir must be refused")
        except BackupError as e:
            assert "live data" in str(e), f"7: {e}"
        assert sha256_file(archive) == archive_sha, "7: a refused restore must not touch the archive"

        # 8. rotation keeps the newest N of this script's own archives, and
        # nothing else in the directory
        (dest / "unrelated.txt").write_text("not a backup")
        create(data, dest, key, stamp="20260101T000003Z")
        removed = rotate(dest, 2)
        left = sorted(p.name for p in dest.glob(f"*{SUFFIX}"))
        assert left == ["clinic-20260101T000001Z.cbk", "clinic-20260101T000003Z.cbk"], f"8: kept {left}"
        assert removed == ["clinic-20260101T000000Z.cbk"], f"8: removed {removed}"
        assert (dest / "unrelated.txt").exists(), "8: rotation must not delete files it did not make"
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
    print("selftest ok")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
