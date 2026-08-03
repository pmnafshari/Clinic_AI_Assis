import queue
import sqlite3
import sys
import threading
import time
import urllib.request
from pathlib import Path

import sort_files
import storage
from auth import log_audit
from action_log import log_action
from extract_note import call_model, parse_reply, OllamaUnreachable

# module-level, selftest-patchable
SORTED_ROOT = Path("sorted")
DROP_DIR = Path("drop")
LOG_PATH = None
DB_PATH = "db/clinic.sqlite"
CHROMA_PATH = "db/chroma"

# own Ollama seam (mirrors app/notes_routes.py) - this is the injection point
# that keeps the selftest offline and deterministic, since route_file's .txt
# branch has no extractor override
_urlopen = urllib.request.urlopen


def _extract(raw_text):
    return parse_reply(call_model(raw_text, urlopen=_urlopen))


_queue = queue.Queue()
_started = False
_start_lock = threading.Lock()


def ensure_started():
    global _started
    with _start_lock:
        if _started:
            return
        thread = threading.Thread(target=_worker_loop, daemon=True)
        thread.start()
        _started = True


def enqueue(path, username, role):
    ensure_started()
    _queue.put((str(path), username, role))


def _original_uploader(conn, path):
    # the upload route writes a queue_upload row before enqueuing, so the
    # uploader survives a restart even though the queue entry does not
    row = conn.execute(
        "SELECT username, role FROM audit_log"
        " WHERE action = 'queue_upload' AND target = ? ORDER BY id DESC LIMIT 1",
        (str(path),),
    ).fetchone()
    if row is None:
        return storage.SYSTEM_USERNAME, storage.SYSTEM_ROLE
    return row["username"], row["role"]


def resume_pending():
    # the queue is in memory and the worker is a daemon thread, so a restart
    # drops everything still waiting. the files are all still in drop/.
    drop = Path(DROP_DIR)
    if not drop.is_dir():
        return 0
    conn = storage.connect(DB_PATH)
    try:
        taken = 0
        for f in sorted(drop.rglob("*")):
            if f.is_file() and not f.name.endswith(".part"):
                username, role = _original_uploader(conn, f)
                enqueue(f, username, role)
                taken += 1
    finally:
        conn.close()
    return taken


def _record_worker_failure(path, username, role, exc):
    # each step guarded on its own: the connection or process that just
    # failed may be the reason this is unreachable too, and a raise here
    # would kill the only worker thread - the same silence this replaces
    try:
        conn = storage.connect(DB_PATH)
        log_audit(conn, username, role, "sync_note", str(path), allowed=0)
        conn.close()
    except Exception as inner:
        print(f"worker failure row lost ({inner}), original: {exc}", file=sys.stderr)
    try:
        log_action(path, "-", f"worker failed: {exc}", LOG_PATH)
    except Exception as inner:
        print(f"worker failure log lost ({inner}), original: {exc}", file=sys.stderr)


def _worker_loop():
    # the app worker is the authoritative processor for uploaded .txt - it
    # alone knows the uploading username. the upload route (Plan 11-03) does
    # an atomic temp+rename hand-off, so this loop only ever sees whole files.
    while True:
        path, username, role = _queue.get()
        try:
            _process_one(path, username, role)
        except Exception as exc:
            _record_worker_failure(path, username, role, exc)
        finally:
            _queue.task_done()


def _process_one(path, username, role):
    src = Path(path)
    conn = storage.connect(DB_PATH)
    try:
        if src.exists():
            dest = sort_files.route_note(src, SORTED_ROOT, LOG_PATH, extract=_extract)
            log_audit(conn, username, role, "upload_file", str(dest), allowed=1)

            json_path = dest.with_suffix(".json")
            # the sibling json is what decides syncability - route_note only
            # writes one on the matched-CF path. keying off dest.suffix
            # instead would skip any note whose extension got mangled.
            if json_path.exists():
                # a failed sync_note row has to carry the same target as the
                # upload_file row above - the intake list collapses by target,
                # and a drop/ path here leaves two rows for one file
                try:
                    collection = storage.get_shared_collection(CHROMA_PATH)
                    status = storage.sync_note_file(
                        json_path, SORTED_ROOT, conn, collection, role, username, target=str(dest)
                    )
                    if status == "failed":
                        log_action(src.name, dest, "sync failed", LOG_PATH)
                except Exception as exc:
                    _record_worker_failure(dest, username, role, exc)
        else:
            # a concurrently-running external watcher grabbed it first - still
            # attribute the upload so it appears in the user's recent-intake
            # list. the name has to be in the target: a fixed string would be
            # one target shared by every such upload, and the intake list
            # collapses by target, so all but the newest would vanish.
            log_audit(conn, username, role, "upload_file", f"routed by watcher: {src.name}", allowed=1)
    finally:
        conn.close()


def selftest():
    import tempfile
    from dental_notes_schema import DentalNote
    from storage import init_db

    global SORTED_ROOT, DROP_DIR, LOG_PATH, DB_PATH, CHROMA_PATH, _extract, _process_one

    VALID_CF = "MRRS800010150100"

    def _fake_ok(text):
        return DentalNote(patient_name="test", codice_fiscale=VALID_CF)

    def _fake_unreachable(text):
        raise OllamaUnreachable("offline")

    orig_sorted_root, orig_drop_dir, orig_log_path, orig_db_path, orig_chroma_path, orig_extract = (
        SORTED_ROOT, DROP_DIR, LOG_PATH, DB_PATH, CHROMA_PATH, _extract
    )
    orig_get_shared_collection = storage.get_shared_collection
    orig_process_one = _process_one
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = str(root / "clinic.sqlite")
            init_db(db_path)

            SORTED_ROOT = root / "sorted"
            LOG_PATH = None
            DB_PATH = db_path
            CHROMA_PATH = str(root / "chroma")

            # 1. matched CF -> success, attributed to aassist
            _extract = _fake_ok
            txt1 = root / "note1.txt"
            txt1.write_text("patient note")
            enqueue(txt1, "aassist", "assistant")

            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            deadline = time.time() + 3.0
            row = None
            while time.time() < deadline:
                rows = conn.execute(
                    "SELECT * FROM audit_log WHERE username=? AND action=? AND allowed=1",
                    ("aassist", "upload_file"),
                ).fetchall()
                if rows:
                    row = rows[0]
                    all_rows = rows
                    break
                time.sleep(0.1)
            assert row is not None, "1: no audit_log row for aassist within deadline"
            assert len(all_rows) == 1, f"1: expected exactly one row, got {len(all_rows)}"
            assert f"{VALID_CF}" in row["target"] and "notes" in row["target"], \
                f"1: target not under sorted/<CF>/notes, got {row['target']}"
            conn.close()

            # 2. OllamaUnreachable -> needs_review, still attributed
            _extract = _fake_unreachable
            txt2 = root / "note2.txt"
            txt2.write_text("another note")
            enqueue(txt2, "drossi", "dentist")

            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            deadline = time.time() + 3.0
            row2 = None
            while time.time() < deadline:
                rows = conn.execute(
                    "SELECT * FROM audit_log WHERE username=? AND action=? AND allowed=1",
                    ("drossi", "upload_file"),
                ).fetchall()
                if rows:
                    row2 = rows[0]
                    all_rows2 = rows
                    break
                time.sleep(0.1)
            assert row2 is not None, "2: no audit_log row for drossi within deadline"
            assert len(all_rows2) == 1, f"2: expected exactly one row, got {len(all_rows2)}"
            assert "needs_review" in row2["target"], f"2: target not in needs_review, got {row2['target']}"
            # 3b. a needs_review .txt has no sibling .json, so it must never sync
            sync_rows_drossi = conn.execute(
                "SELECT 1 FROM audit_log WHERE username=? AND action=?",
                ("drossi", "sync_note"),
            ).fetchall()
            assert len(sync_rows_drossi) == 0, "3b: needs_review upload produced a sync_note row"
            conn.close()

            # 3. matched CF, sync succeeds -> sync_note row landed, attributed to the
            # uploader, sharing its target with the upload_file row, and a visits row lands
            _extract = _fake_ok
            txt3 = root / "note3.txt"
            txt3.write_text("patient note three")
            enqueue(txt3, "bbianchi", "assistant")

            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            deadline = time.time() + 3.0
            sync_row = None
            while time.time() < deadline:
                rows = conn.execute(
                    "SELECT * FROM audit_log WHERE username=? AND action=?",
                    ("bbianchi", "sync_note"),
                ).fetchall()
                if rows:
                    sync_row = rows[0]
                    break
                time.sleep(0.1)
            assert sync_row is not None, "3: no sync_note row for bbianchi within deadline"
            assert sync_row["allowed"] == 1, f"3: expected allowed=1, got {sync_row['allowed']}"

            upload_row = conn.execute(
                "SELECT * FROM audit_log WHERE username=? AND action=?",
                ("bbianchi", "upload_file"),
            ).fetchone()
            assert upload_row is not None, "3: no upload_file row for bbianchi"
            assert sync_row["target"] == upload_row["target"], (
                f"3: sync_note target {sync_row['target']!r} != "
                f"upload_file target {upload_row['target']!r}"
            )

            source_path = str(Path(sync_row["target"]).with_suffix(".json").relative_to(SORTED_ROOT))
            visit_row = conn.execute(
                "SELECT 1 FROM visits WHERE source_path = ?", (source_path,)
            ).fetchone()
            assert visit_row is not None, "3: no visits row landed for the synced note"
            conn.close()

            # 4. sync failure (Chroma upsert raises) -> sync_note allowed=0, log.txt line,
            # file stays where route_note filed it
            class _FailingCollection:
                def upsert(self, **kwargs):
                    raise RuntimeError("upsert boom")

                def get(self, ids=None):
                    return {"ids": []}

            storage.get_shared_collection = lambda path: _FailingCollection()
            log_path4 = str(root / "log4.txt")
            LOG_PATH = log_path4
            txt4 = root / "note4.txt"
            txt4.write_text("patient note four")
            enqueue(txt4, "cverdi", "assistant")

            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            deadline = time.time() + 3.0
            fail_row = None
            while time.time() < deadline:
                rows = conn.execute(
                    "SELECT * FROM audit_log WHERE username=? AND action=?",
                    ("cverdi", "sync_note"),
                ).fetchall()
                if rows:
                    fail_row = rows[0]
                    break
                time.sleep(0.1)
            assert fail_row is not None, "4: no sync_note row for cverdi within deadline"
            assert fail_row["allowed"] == 0, f"4: expected allowed=0, got {fail_row['allowed']}"
            conn.close()

            with open(log_path4) as f:
                log_text = f.read()
            assert "sync failed" in log_text, "4: log.txt has no 'sync failed' line"
            assert (SORTED_ROOT / VALID_CF / "notes" / "note4.txt").exists(), \
                "4: a sync failure must not move the filed note"

            # 5. an arbitrary exception in _process_one is recorded, not swallowed,
            # and the thread keeps serving the next item
            def _boom(path, username, role):
                raise RuntimeError("boom")

            _process_one = _boom
            enqueue(root / "note5.txt", "eneri", "assistant")

            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            deadline = time.time() + 3.0
            worker_fail_row = None
            while time.time() < deadline:
                rows = conn.execute(
                    "SELECT * FROM audit_log WHERE username=? AND action=? AND allowed=0",
                    ("eneri", "sync_note"),
                ).fetchall()
                if rows:
                    worker_fail_row = rows[0]
                    break
                time.sleep(0.1)
            assert worker_fail_row is not None, "5: no failed sync_note row for eneri within deadline"
            conn.close()

            _process_one = orig_process_one
            LOG_PATH = None
            txt6 = root / "note6.txt"
            txt6.write_text("patient note six")
            enqueue(txt6, "fgalli", "assistant")

            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            deadline = time.time() + 3.0
            survived_row = None
            while time.time() < deadline:
                rows = conn.execute(
                    "SELECT * FROM audit_log WHERE username=? AND action=? AND allowed=1",
                    ("fgalli", "upload_file"),
                ).fetchall()
                if rows:
                    survived_row = rows[0]
                    break
                time.sleep(0.1)
            assert survived_row is not None, "5: worker thread did not process the next item after a failure"
            conn.close()

            # 6. the collection open itself raises (mirrors chmod -w db/chroma), before
            # sync_note_file is ever entered - still exactly one sync_note row, and its
            # target must match the routed upload_file row, not the enqueued drop path
            def _boom_open(path):
                raise RuntimeError("chroma open boom")

            storage.get_shared_collection = _boom_open
            log_path6 = str(root / "log6.txt")
            LOG_PATH = log_path6
            _extract = _fake_ok
            txt_defect2 = root / "note_defect2.txt"
            txt_defect2.write_text("patient note defect2")
            enqueue(txt_defect2, "ghirsch", "assistant")

            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            deadline = time.time() + 3.0
            sync_row6 = None
            while time.time() < deadline:
                rows = conn.execute(
                    "SELECT * FROM audit_log WHERE username=? AND action=?",
                    ("ghirsch", "sync_note"),
                ).fetchall()
                if rows:
                    sync_row6 = rows[0]
                    all_rows6 = rows
                    break
                time.sleep(0.1)
            assert sync_row6 is not None, "6: no sync_note row for ghirsch within deadline"
            assert len(all_rows6) == 1, f"6: expected exactly one sync_note row, got {len(all_rows6)}"
            assert sync_row6["allowed"] == 0, f"6: expected allowed=0, got {sync_row6['allowed']}"

            upload_row6 = conn.execute(
                "SELECT * FROM audit_log WHERE username=? AND action=?",
                ("ghirsch", "upload_file"),
            ).fetchone()
            assert upload_row6 is not None, "6: no upload_file row for ghirsch"
            assert sync_row6["target"] == upload_row6["target"], (
                "6: sync failure must be recorded against the routed target, not the enqueued path"
            )
            assert sync_row6["target"].startswith(str(SORTED_ROOT)), (
                f"6: target not routed under SORTED_ROOT, got {sync_row6['target']!r}"
            )
            conn.close()

            assert (SORTED_ROOT / VALID_CF / "notes" / "note_defect2.txt").exists(), \
                "6: a sync failure must not move the filed note"

            # 7. a restart drops the in-memory queue, but whatever is still in
            # drop/ gets picked up again under the account that uploaded it
            storage.get_shared_collection = orig_get_shared_collection
            _extract = _fake_ok
            LOG_PATH = None
            drop7 = root / "drop7"
            drop7.mkdir()
            DROP_DIR = drop7
            leftover = drop7 / "leftover7.txt"
            leftover.write_text("patient note leftover")
            (drop7 / "halfwritten.part").write_text("not finished")

            conn = storage.connect(db_path)
            log_audit(conn, "hleft", "assistant", "queue_upload", str(leftover), allowed=1)
            conn.close()

            taken = resume_pending()
            assert taken == 1, f"7: expected 1 leftover taken (the .part is not one), got {taken}"

            deadline = time.time() + 3.0
            resumed_row = None
            while time.time() < deadline:
                conn = storage.connect(db_path)
                resumed_row = conn.execute(
                    "SELECT * FROM audit_log WHERE username=? AND action=? AND allowed=1",
                    ("hleft", "upload_file"),
                ).fetchone()
                conn.close()
                if resumed_row:
                    break
                time.sleep(0.1)
            assert resumed_row is not None, "7: leftover drop file was never re-enqueued"
            assert str(SORTED_ROOT) in resumed_row["target"], \
                f"7: leftover not routed under sorted, got {resumed_row['target']}"
            assert (drop7 / "halfwritten.part").exists(), "7: a .part must be left alone"
    finally:
        storage.get_shared_collection = orig_get_shared_collection
        _process_one = orig_process_one
        SORTED_ROOT, DROP_DIR, LOG_PATH, DB_PATH, CHROMA_PATH, _extract = (
            orig_sorted_root, orig_drop_dir, orig_log_path, orig_db_path, orig_chroma_path, orig_extract
        )

    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    print("usage: python upload_worker.py --selftest")


if __name__ == "__main__":
    main()
