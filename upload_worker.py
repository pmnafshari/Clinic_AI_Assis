import queue
import sqlite3
import sys
import threading
import time
import urllib.request
from pathlib import Path

import sort_files
from auth import log_audit
from extract_note import call_model, parse_reply, OllamaUnreachable

# module-level, selftest-patchable
SORTED_ROOT = Path("sorted")
LOG_PATH = None
DB_PATH = "db/clinic.sqlite"

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


def _worker_loop():
    # the app worker is the authoritative processor for uploaded .txt - it
    # alone knows the uploading username. the upload route (Plan 11-03) does
    # an atomic temp+rename hand-off, so this loop only ever sees whole files.
    while True:
        path, username, role = _queue.get()
        try:
            _process_one(path, username, role)
        except Exception:
            pass
        finally:
            _queue.task_done()


def _process_one(path, username, role):
    src = Path(path)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        if src.exists():
            dest = sort_files.route_note(src, SORTED_ROOT, LOG_PATH, extract=_extract)
            log_audit(conn, username, role, "upload_file", str(dest), allowed=1)
        else:
            # a concurrently-running external watcher grabbed it first - still
            # attribute the upload so it appears in the user's recent-intake list
            log_audit(conn, username, role, "upload_file", "routed by watcher", allowed=1)
    finally:
        conn.close()


def selftest():
    import tempfile
    from dental_notes_schema import DentalNote
    from storage import init_db

    global SORTED_ROOT, LOG_PATH, DB_PATH, _extract

    VALID_CF = "MRRS800010150100"

    def _fake_ok(text):
        return DentalNote(patient_name="test", codice_fiscale=VALID_CF)

    def _fake_unreachable(text):
        raise OllamaUnreachable("offline")

    orig_sorted_root, orig_log_path, orig_db_path, orig_extract = SORTED_ROOT, LOG_PATH, DB_PATH, _extract
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = str(root / "clinic.sqlite")
            init_db(db_path)

            SORTED_ROOT = root / "sorted"
            LOG_PATH = None
            DB_PATH = db_path

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
            conn.close()
    finally:
        SORTED_ROOT, LOG_PATH, DB_PATH, _extract = orig_sorted_root, orig_log_path, orig_db_path, orig_extract

    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    print("usage: python upload_worker.py --selftest")


if __name__ == "__main__":
    main()
