import sqlite3
import sys
import time
import tempfile
from pathlib import Path

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from sort_files import route_file
from extract_note import extract_note
from action_log import log_action
import storage

# module-level, selftest-patchable seams. resolved against this file, not the
# CWD - the watcher is started from anywhere and a relative path would just
# create an empty db next to wherever it was launched
DB_PATH = str(Path(__file__).parent / "db" / "clinic.sqlite")
CHROMA_PATH = str(Path(__file__).parent / "db" / "chroma")
_extract = extract_note


def sync_dropped(dest, sorted_root, log_path):
    # only a filed .txt with a sibling .json went through route_note's
    # matched-CF path - media files and needs_review notes get no sync (D-09)
    if dest is None or dest.suffix.lower() != ".txt":
        return
    json_path = dest.with_suffix(".json")
    if not json_path.exists():
        return

    conn = None
    try:
        conn = storage.connect(DB_PATH)
        collection = storage.get_shared_collection(CHROMA_PATH)
        status = storage.sync_note_file(
            json_path, sorted_root, conn, collection, storage.SYSTEM_ROLE, storage.SYSTEM_USERNAME
        )
        if status == "failed":
            log_action(dest, "-", "sync failed", log_path)
    except Exception as exc:
        # a daemon must not die on one bad note
        log_action(dest, "-", f"sync failed: {exc}", log_path)
    finally:
        if conn is not None:
            conn.close()


def route_and_sync(src, sorted_root, log_path):
    # route_note only catches OllamaUnreachable and ValueError. anything else
    # (unreadable file, a race with the upload worker) would propagate into
    # watchdog's dispatcher thread and kill it while watch() keeps sleeping,
    # so the daemon looks healthy and silently stops filing everything.
    try:
        dest = route_file(src, sorted_root, log_path, extract=_extract)
    except Exception as exc:
        log_action(src, "-", f"route failed: {exc}", log_path)
        return
    sync_dropped(dest, sorted_root, log_path)


class DropHandler(FileSystemEventHandler):
    def __init__(self, sorted_root, log_path=None):
        self.sorted_root = sorted_root
        self.log_path = log_path

    def on_created(self, event):
        if event.is_directory:
            return
        # debounce: wait for partial/large writes to settle
        time.sleep(0.5)
        src = Path(event.src_path)
        if src.exists():
            route_and_sync(src, self.sorted_root, self.log_path)


def watch(drop_dir, sorted_root, log_path=None):
    drop_dir = Path(drop_dir)
    sorted_root = Path(sorted_root)

    # the app may never have run here - without this every audit and upsert
    # below fails with "no such table" and is swallowed by sync_dropped
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    storage.init_db(DB_PATH).close()

    # startup catch-up: route files already present before watcher started
    for f in drop_dir.rglob("*"):
        if f.is_file():
            route_and_sync(f, sorted_root, log_path)

    handler = DropHandler(sorted_root, log_path)
    observer = Observer()
    observer.schedule(handler, path=str(drop_dir), recursive=True)
    observer.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()


def selftest():
    # offline — uses .xlsx with CF in filename (find_cf_in_name, no model call)
    from dental_notes_schema import DentalNote

    global DB_PATH, CHROMA_PATH, _extract

    CF = "MRRS800010150100"

    def _fake_extract(text):
        return DentalNote(patient_name="test", codice_fiscale=CF)

    orig_db_path, orig_chroma_path, orig_extract = DB_PATH, CHROMA_PATH, _extract

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        drop = root / "drop"
        drop.mkdir()
        sorted_ = root / "sorted"
        # staging area outside the watched dir: blocks 4 and 5 route by hand,
        # and a file written into drop/ would race the live observer for it
        stage = root / "stage"
        stage.mkdir()
        log_path = str(root / "watch.log")
        db_path = str(root / "clinic.sqlite")
        storage.init_db(db_path).close()

        observer = None
        try:
            DB_PATH = db_path
            CHROMA_PATH = str(root / "chroma")
            _extract = _fake_extract

            # 1. startup catch-up: file present before watcher starts is routed
            pre = drop / f"fattura_{CF}_2026.xlsx"
            pre.write_bytes(b"PK")

            handler = DropHandler(sorted_, log_path)
            observer = Observer()
            observer.schedule(handler, path=str(drop), recursive=True)
            observer.start()

            # run startup catch-up manually (watch() does it; selftest calls it directly)
            for f in drop.rglob("*"):
                if f.is_file():
                    route_and_sync(f, sorted_, log_path)

            # wait briefly so any observer init settles
            time.sleep(0.2)

            assert (sorted_ / CF / "records" / pre.name).exists(), "1: startup catch-up file not routed"

            # 2. new file dropped while watcher is live routes within 2 seconds
            new_file = drop / f"rx_{CF}.jpg"
            new_file.write_bytes(b"\xff\xd8\xff")

            deadline = time.time() + 2.0
            routed = sorted_ / CF / "images" / new_file.name
            while time.time() < deadline:
                if routed.exists():
                    break
                time.sleep(0.1)
            assert routed.exists(), "2: new file not routed within 2 seconds"

            # 3. recursive: file dropped into a nested subfolder is detected
            sub = drop / "inbox" / "sub"
            sub.mkdir(parents=True, exist_ok=True)
            nested = sub / f"preventivo_{CF}_q1.xlsx"
            nested.write_bytes(b"PK")

            deadline = time.time() + 2.0
            routed_nested = sorted_ / CF / "records" / nested.name
            while time.time() < deadline:
                if routed_nested.exists():
                    break
                time.sleep(0.1)
            assert routed_nested.exists(), "3: nested subfolder file not routed within 2 seconds"

            # 4. a .txt present before start is routed and synced by the catch-up loop
            pre_note = stage / "note_pre.txt"
            pre_note.write_text("patient note")
            dest4 = route_file(pre_note, sorted_, log_path, extract=_extract)
            sync_dropped(dest4, sorted_, log_path)

            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            json_path4 = dest4.with_suffix(".json")
            source_path4 = str(json_path4.relative_to(sorted_))
            visit_row = conn.execute(
                "SELECT 1 FROM visits WHERE source_path = ?", (source_path4,)
            ).fetchone()
            assert visit_row is not None, "4: catch-up did not sync the pre-existing note"
            sync_row = conn.execute(
                "SELECT * FROM audit_log WHERE action='sync_note' AND target=?", (str(json_path4),)
            ).fetchone()
            assert sync_row is not None, "4: no sync_note row for the catch-up note"
            assert sync_row["username"] == "system", f"4: expected username=system, got {sync_row['username']}"
            assert sync_row["role"] == "system", f"4: expected role=system, got {sync_row['role']}"
            assert sync_row["allowed"] == 1, f"4: expected allowed=1, got {sync_row['allowed']}"
            conn.close()

            # 4b. a .txt dropped while the observer is live is synced within the deadline poll
            live_note = drop / "note_live.txt"
            live_note.write_text("patient note live")

            deadline = time.time() + 2.0
            sync_row_live = None
            while time.time() < deadline:
                conn = sqlite3.connect(db_path)
                conn.row_factory = sqlite3.Row
                sync_row_live = conn.execute(
                    "SELECT * FROM audit_log WHERE action='sync_note' AND target LIKE ?",
                    (f"%{live_note.stem}%",),
                ).fetchone()
                conn.close()
                if sync_row_live:
                    break
                time.sleep(0.1)
            assert sync_row_live is not None, "4b: live-dropped note not synced within deadline"
            assert sync_row_live["username"] == "system", "4b: live sync not attributed to system"

            # 4c. the .jpg from block 2 produced no sync_note row (D-09)
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            jpg_sync = conn.execute(
                "SELECT 1 FROM audit_log WHERE action='sync_note' AND target LIKE ?",
                (f"%{new_file.name}%",),
            ).fetchone()
            assert jpg_sync is None, "4c: .jpg produced a sync_note row"
            conn.close()

            # 5. sync failure -> a log.txt line, sync_dropped does not raise
            class _FailingCollection:
                def upsert(self, **kwargs):
                    raise RuntimeError("upsert boom")

                def get(self, ids=None):
                    return {"ids": []}

            orig_get_shared_collection = storage.get_shared_collection
            storage.get_shared_collection = lambda path: _FailingCollection()
            try:
                fail_note = stage / "note_fail.txt"
                fail_note.write_text("patient note fail")
                dest_fail = route_file(fail_note, sorted_, log_path, extract=_extract)
                sync_dropped(dest_fail, sorted_, log_path)
            finally:
                storage.get_shared_collection = orig_get_shared_collection

            with open(log_path) as f:
                log_text = f.read()
            assert "sync failed" in log_text, "5: log.txt has no 'sync failed' line"

            # 6. a route failure the sorter does not catch must be logged and
            # swallowed, not raised into watchdog's dispatcher thread
            def _fake_boom(text):
                raise RuntimeError("route boom")

            _extract = _fake_boom
            bad = drop / "note_bad.txt"
            bad.write_text("boom")

            deadline = time.time() + 5.0
            while time.time() < deadline:
                with open(log_path) as f:
                    if "route failed" in f.read():
                        break
                time.sleep(0.1)
            with open(log_path) as f:
                assert "route failed" in f.read(), "6: no 'route failed' line for the bad file"

            _extract = _fake_extract
            after_bad = drop / f"rx_after_bad_{CF}.jpg"
            after_bad.write_bytes(b"\xff\xd8\xff")

            deadline = time.time() + 5.0
            routed_after = sorted_ / CF / "images" / after_bad.name
            while time.time() < deadline:
                if routed_after.exists():
                    break
                time.sleep(0.1)
            assert routed_after.exists(), "6: dispatcher stopped routing after one bad file"
        finally:
            # in the try, a failed assertion would leak a live observer into
            # the TemporaryDirectory teardown
            if observer is not None:
                observer.stop()
                observer.join()
            DB_PATH, CHROMA_PATH, _extract = orig_db_path, orig_chroma_path, orig_extract

    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    if len(sys.argv) < 2:
        print("usage: python watcher.py <drop_dir> [sorted_dir]  |  python watcher.py --selftest")
        sys.exit(1)
    drop_dir = sys.argv[1]
    sorted_root = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("sorted")
    log_path = str(sorted_root / "log.txt")
    watch(drop_dir, sorted_root, log_path)


if __name__ == "__main__":
    main()
