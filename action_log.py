import re
import sys
from datetime import datetime
from pathlib import Path

LOG_FILE = "log.txt"

# the shape of a codice fiscale, real or synthetic, in any case. this log is a
# plain file with no access control and no erasure path, so it never holds one
# (P06.04) - nor the machine's absolute paths, which name the user's home.
CF_SHAPE = re.compile(
    r"(?<![A-Za-z0-9])([A-Za-z]{4}[0-9]{12}|[A-Za-z]{6}[0-9LMNPQRSTUV]{2}[A-Za-z]"
    r"[0-9LMNPQRSTUV]{2}[A-Za-z][0-9LMNPQRSTUV]{3}[A-Za-z])(?![A-Za-z0-9])")


def _clean(value):
    text = str(value)
    if "/" in text:
        text = "/".join(Path(text).parts[-3:])
    return CF_SHAPE.sub("<cf>", text)


def log_action(src, dest, reason, log_path=None):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} | {_clean(src)} | {_clean(dest)} | {CF_SHAPE.sub('<cf>', str(reason))}\n"
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

        # 3. no codice fiscale and no absolute path reaches the file
        log_action("/Users/someone/clinic/drop/fattura_MRRS800010150100_2026.xlsx",
                   "/Users/someone/clinic/sorted/pid_0a1b2c3d4e5f6a7b/records/f.xlsx",
                   "type:xlsx cf:RSSMRA85T10A562S", log_path)
        last = open(log_path).read().splitlines()[-1]
        assert "MRRS800010150100" not in last and "RSSMRA85T10A562S" not in last, \
            f"3: a codice fiscale reached the log: {last}"
        assert "/Users/" not in last, f"3: an absolute path reached the log: {last}"
        assert "sorted/pid_0a1b2c3d4e5f6a7b/records" not in last or "f.xlsx" in last, last
        assert "pid_0a1b2c3d4e5f6a7b" in last, "3: the surrogate directory is kept"

    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    print("usage: python action_log.py --selftest")
    sys.exit(1)


if __name__ == "__main__":
    main()
