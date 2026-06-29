import sys
import time
import tempfile
from pathlib import Path

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from sort_files import route_file


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
            route_file(src, self.sorted_root, self.log_path)


def watch(drop_dir, sorted_root, log_path=None):
    drop_dir = Path(drop_dir)
    sorted_root = Path(sorted_root)

    # startup catch-up: route files already present before watcher started
    for f in drop_dir.rglob("*"):
        if f.is_file():
            route_file(f, sorted_root, log_path)

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
    CF = "MRRS800010150100"

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        drop = root / "drop"
        drop.mkdir()
        sorted_ = root / "sorted"

        # 1. startup catch-up: file present before watcher starts is routed
        pre = drop / f"fattura_{CF}_2026.xlsx"
        pre.write_bytes(b"PK")

        handler = DropHandler(sorted_)
        observer = Observer()
        observer.schedule(handler, path=str(drop), recursive=True)
        observer.start()

        # run startup catch-up manually (watch() does it; selftest calls it directly)
        for f in drop.rglob("*"):
            if f.is_file():
                route_file(f, sorted_)

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

        observer.stop()
        observer.join()

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
