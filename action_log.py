import sys
from datetime import datetime
from pathlib import Path

LOG_FILE = "log.txt"


def log_action(src, dest, reason, log_path=None):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} | {src} | {dest} | {reason}\n"
    with open(log_path or LOG_FILE, "a") as f:
        f.write(line)


def selftest():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        log_path = str(Path(tmp) / "test.log")

        # 1. two successive calls append two lines (append mode, not truncate)
        log_action("drop/note1.txt", "sorted/MRRS800010150100/notes/note1.txt", "matched CF", log_path)
        log_action("drop/note2.txt", "sorted/needs_review/note2.txt", "extract failed", log_path)

        with open(log_path) as f:
            lines = f.readlines()

        assert len(lines) == 2, f"expected 2 lines, got {len(lines)}"

        # 2. each line splits on " | " into exactly 4 fields, field 0 parses as timestamp
        for i, line in enumerate(lines, 1):
            parts = line.rstrip("\n").split(" | ")
            assert len(parts) == 4, f"line {i}: expected 4 fields, got {len(parts)}: {line!r}"
            datetime.strptime(parts[0], "%Y-%m-%d %H:%M:%S")

    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    print("usage: python action_log.py --selftest")
    sys.exit(1)


if __name__ == "__main__":
    main()
