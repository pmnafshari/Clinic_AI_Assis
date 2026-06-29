import os
import re
import shutil
import sys
from pathlib import Path

from dental_notes_schema import CF_PATTERN
from extract_note import extract_note, OllamaUnreachable
from action_log import log_action


def _move(src, dest_dir, reason, sorted_root, log_path=None):
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    n = 1
    while dest.exists():
        dest = dest_dir / f"{src.stem}_{n}{src.suffix}"
        n += 1
    # security: destination must stay inside sorted_root
    resolved_root = str(sorted_root.resolve())
    resolved_dest = str(dest.resolve())
    if not resolved_dest.startswith(resolved_root + os.sep):
        raise ValueError("path escape: destination is outside sorted root")
    shutil.move(str(src), str(dest))
    log_action(src, dest, reason, log_path)


def route_note(src, sorted_root, log_path=None, extract=extract_note):
    # symlink guard — never follow
    if src.is_symlink():
        _move(src, sorted_root / "needs_review", "symlink skipped", sorted_root, log_path)
        return
    try:
        note = extract(src.read_text())
        cf = note.codice_fiscale  # already validated by DentalNote
        _move(src, sorted_root / cf / "notes", "matched CF", sorted_root, log_path)
    except OllamaUnreachable as e:
        _move(src, sorted_root / "needs_review", str(e), sorted_root, log_path)
    except ValueError as e:
        _move(src, sorted_root / "needs_review", "extract_note rejected: " + str(e), sorted_root, log_path)


def selftest():
    import tempfile
    from dental_notes_schema import DentalNote

    VALID_CF = "MRRS800010150100"

    def make_extractor(cf=None, error=None):
        def extract(text):
            if error is not None:
                raise error
            return DentalNote(patient_name="test", codice_fiscale=cf)
        return extract

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        sorted_ = root / "sorted"
        log_path = str(root / "test.log")

        # 1. valid CF → sorted/<CF>/notes/<name>, log contains "matched CF"
        f1 = root / "note1.txt"
        f1.write_text("patient note")
        route_note(f1, sorted_, log_path=log_path, extract=make_extractor(cf=VALID_CF))
        assert (sorted_ / VALID_CF / "notes" / "note1.txt").exists(), "1: file not filed by CF"
        with open(log_path) as f:
            lines = f.readlines()
        assert any("matched CF" in l for l in lines), "1: 'matched CF' not in log"

        # 2. ValueError → needs_review, reason in log
        f2 = root / "note2.txt"
        f2.write_text("bad note")
        route_note(f2, sorted_, log_path=log_path, extract=make_extractor(error=ValueError("bad json")))
        assert (sorted_ / "needs_review" / "note2.txt").exists(), "2: not in needs_review"
        with open(log_path) as f:
            lines = f.readlines()
        assert any("bad json" in l for l in lines), "2: ValueError reason not logged"

        # 3. OllamaUnreachable → needs_review, reason in log
        f3 = root / "note3.txt"
        f3.write_text("another note")
        route_note(f3, sorted_, log_path=log_path, extract=make_extractor(error=OllamaUnreachable("offline")))
        assert (sorted_ / "needs_review" / "note3.txt").exists(), "3: not in needs_review"
        with open(log_path) as f:
            lines = f.readlines()
        assert any("offline" in l for l in lines), "3: OllamaUnreachable reason not logged"

        # 4. collision → second file becomes *_1.txt
        fa = root / "collision.txt"
        fa.write_text("first")
        route_note(fa, sorted_, log_path=log_path, extract=make_extractor(cf=VALID_CF))
        fb = root / "collision.txt"  # fa was moved; create a new file with the same name
        fb.write_text("second")
        route_note(fb, sorted_, log_path=log_path, extract=make_extractor(cf=VALID_CF))
        assert (sorted_ / VALID_CF / "notes" / "collision_1.txt").exists(), "4: collision not renamed to _1"

        # 5. symlink source → needs_review with "symlink skipped" in log
        real = root / "real.txt"
        real.write_text("real content")
        link = root / "symlink_note.txt"
        link.symlink_to(real)
        route_note(link, sorted_, log_path=log_path)
        assert (sorted_ / "needs_review" / "symlink_note.txt").exists(), "5: symlink not in needs_review"
        with open(log_path) as f:
            lines = f.readlines()
        assert any("symlink skipped" in l for l in lines), "5: 'symlink skipped' not logged"

        # 6. all non-symlink files stay within sorted root
        sorted_abs = str(sorted_.resolve())
        for p in sorted_.rglob("*"):
            if p.is_file() and not p.is_symlink():
                assert str(p.resolve()).startswith(sorted_abs + os.sep), f"6: {p} escaped root"

        # 7. find_cf_in_name — hit: CF embedded in filename
        assert find_cf_in_name(f"fattura_{VALID_CF}_2026.xlsx") == VALID_CF, "7: CF not found in name"

        # 8. find_cf_in_name — miss: no CF token in filename
        assert find_cf_in_name("fattura_generica.xlsx") is None, "8: unexpected CF found"

        # 9. xlsx with CF in filename → sorted/<CF>/records/
        f9 = root / f"fattura_{VALID_CF}_2026.xlsx"
        f9.write_bytes(b"PK")
        route_file(f9, sorted_, log_path)
        assert (sorted_ / VALID_CF / "records" / f9.name).exists(), "9: xlsx+CF not in patient records"

        # 10. xlsx without CF → sorted/records/
        f10 = root / "fattura_generica.xlsx"
        f10.write_bytes(b"PK")
        route_file(f10, sorted_, log_path)
        assert (sorted_ / "records" / "fattura_generica.xlsx").exists(), "10: xlsx no-cf not in top-level records"

        # 11. jpg with CF in filename → sorted/<CF>/images/
        f11 = root / f"rx_{VALID_CF}.jpg"
        f11.write_bytes(b"\xff\xd8\xff")
        route_file(f11, sorted_, log_path)
        assert (sorted_ / VALID_CF / "images" / f11.name).exists(), "11: jpg+CF not in patient images"

        # 12. png without CF → sorted/images/
        f12 = root / "rx_generica.png"
        f12.write_bytes(b"\x89PNG")
        route_file(f12, sorted_, log_path)
        assert (sorted_ / "images" / "rx_generica.png").exists(), "12: png no-cf not in top-level images"

        # 13. unknown extension → sorted/needs_review/
        f13 = root / "unknown_file.dat"
        f13.write_bytes(b"data")
        route_file(f13, sorted_, log_path)
        assert (sorted_ / "needs_review" / "unknown_file.dat").exists(), "13: unknown ext not in needs_review"

    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    if len(sys.argv) < 2:
        print("usage: python sort_files.py <note.txt> [sorted_root]  |  python sort_files.py --selftest")
        sys.exit(1)
    src = Path(sys.argv[1])
    sorted_root = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("sorted")
    route_note(src, sorted_root)


if __name__ == "__main__":
    main()
