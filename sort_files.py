import os
import re
import shutil
import sys
from pathlib import Path

from dental_notes_schema import CF_PATTERN
from extract_note import extract_note, OllamaUnreachable
from action_log import log_action

# the reason carried into audit_log, shown to staff on the intake list, AND
# written to log.txt (since phase 26 - it used to get the raw exception text).
# both sinks now share this vocabulary; do not reintroduce free text on either.
# a CLOSED vocabulary on purpose: extract_note's ValueError text can contain a
# codice fiscale (dental_notes_schema raises "got {v!r}"), and this value is
# stored per-user and rendered into HTML. log.txt keeps the detailed reason;
# this one never interpolates an exception.
REASON_EXTRACT_FAILED = "the model could not read this note"
REASON_MODEL_UNREACHABLE = "the local model was unreachable"
REASON_SYMLINK = "skipped: the file was a shortcut, not a real file"
REASON_UNSUPPORTED_TYPE = "unsupported file type"

NEEDS_REVIEW_REASONS = frozenset({
    REASON_EXTRACT_FAILED,
    REASON_MODEL_UNREACHABLE,
    REASON_SYMLINK,
    REASON_UNSUPPORTED_TYPE,
})


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
    return dest


def find_cf_in_name(filename):
    for token in re.split(r'[\W_]+', filename.upper()):
        if CF_PATTERN.match(token):
            return token
    return None


def route_note_reasoned(src, sorted_root, log_path=None, extract=extract_note):
    # returns (dest, reason). reason is None on the success path and a constant
    # from NEEDS_REVIEW_REASONS otherwise. the log_path reason stays detailed.
    # symlink guard — never follow
    if src.is_symlink():
        dest = _move(src, sorted_root / "needs_review", REASON_SYMLINK, sorted_root, log_path)
        return dest, REASON_SYMLINK
    try:
        note = extract(src.read_text())
        cf = note.codice_fiscale  # already validated by DentalNote
        dest = _move(src, sorted_root / cf / "notes", "matched CF", sorted_root, log_path)
        dest.with_suffix(".json").write_text(note.model_dump_json())
        return dest, None
    except OllamaUnreachable:
        dest = _move(src, sorted_root / "needs_review", REASON_MODEL_UNREACHABLE, sorted_root, log_path)
        return dest, REASON_MODEL_UNREACHABLE
    except ValueError:
        # NOT "extract_note rejected: " + str(e). dental_notes_schema raises
        # "got {v!r}" carrying the codice fiscale, and this string lands in
        # log.txt in plaintext. the CF is already in the dest column on the
        # same line, so the interpolation was duplication, not diagnosis.
        dest = _move(src, sorted_root / "needs_review", REASON_EXTRACT_FAILED, sorted_root, log_path)
        return dest, REASON_EXTRACT_FAILED


def route_note(src, sorted_root, log_path=None, extract=extract_note):
    return route_note_reasoned(src, sorted_root, log_path, extract=extract)[0]


def route_file_reasoned(src, sorted_root, log_path=None, extract=extract_note):
    # returns (dest, reason). see route_note_reasoned.
    # symlink guard — never follow, for any file type
    if src.is_symlink():
        dest = _move(src, sorted_root / "needs_review", REASON_SYMLINK, sorted_root, log_path)
        return dest, REASON_SYMLINK
    ext = src.suffix.lower()
    if ext == ".txt":
        return route_note_reasoned(src, sorted_root, log_path, extract=extract)
    cf = find_cf_in_name(src.name)
    if ext == ".xlsx":
        sub = "records"
    elif ext == ".pdf":
        sub = "documents"
    elif ext in (".jpg", ".jpeg", ".png"):
        sub = "images"
    else:
        dest = _move(src, sorted_root / "needs_review", REASON_UNSUPPORTED_TYPE, sorted_root, log_path)
        return dest, REASON_UNSUPPORTED_TYPE
    if cf:
        dest_dir = sorted_root / cf / sub
        reason = f"type:{ext.lstrip('.')} cf:{cf}"
    else:
        dest_dir = sorted_root / sub
        reason = f"type:{ext.lstrip('.')} no-cf"
    return _move(src, dest_dir, reason, sorted_root, log_path), None


def route_file(src, sorted_root, log_path=None, extract=extract_note):
    return route_file_reasoned(src, sorted_root, log_path, extract=extract)[0]


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

        # 1b. matched CF also leaves a sibling .json that round-trips
        note1_json = sorted_ / VALID_CF / "notes" / "note1.json"
        assert note1_json.exists(), "1b: sibling json not written for matched CF"
        reloaded = DentalNote.model_validate_json(note1_json.read_text())
        assert reloaded.codice_fiscale == VALID_CF, "1b: reloaded json has wrong codice_fiscale"

        # 2. ValueError → needs_review, CATEGORY in log (not the exception text).
        # changed in phase 26: this used to assert "bad json" reached log.txt.
        # it did, and so did any codice fiscale the exception carried.
        f2 = root / "note2.txt"
        f2.write_text("bad note")
        route_note(f2, sorted_, log_path=log_path, extract=make_extractor(error=ValueError("bad json")))
        assert (sorted_ / "needs_review" / "note2.txt").exists(), "2: not in needs_review"
        with open(log_path) as f:
            lines = f.readlines()
        note2_lines = [l for l in lines if "note2.txt" in l]
        assert note2_lines, "2: no log line written for the rejected note"
        assert any(REASON_EXTRACT_FAILED in l for l in note2_lines), \
            "2: the categorical reason should be logged"
        assert not any("bad json" in l for l in note2_lines), \
            "2: the exception text must NOT reach log.txt"
        assert not (sorted_ / "needs_review" / "note2.json").exists(), "2: needs_review note must not get a json"

        # 3. OllamaUnreachable → needs_review, reason in log
        f3 = root / "note3.txt"
        f3.write_text("another note")
        route_note(f3, sorted_, log_path=log_path, extract=make_extractor(error=OllamaUnreachable("offline")))
        assert (sorted_ / "needs_review" / "note3.txt").exists(), "3: not in needs_review"
        with open(log_path) as f:
            lines = f.readlines()
        note3_lines = [l for l in lines if "note3.txt" in l]
        assert note3_lines, "3: no log line written for the unreachable-model note"
        assert any(REASON_MODEL_UNREACHABLE in l for l in note3_lines), \
            "3: the categorical reason should be logged"
        assert not any("offline" in l for l in note3_lines), \
            "3: the exception text must NOT reach log.txt"

        # 4. collision → second file becomes *_1.txt
        fa = root / "collision.txt"
        fa.write_text("first")
        route_note(fa, sorted_, log_path=log_path, extract=make_extractor(cf=VALID_CF))
        fb = root / "collision.txt"  # fa was moved; create a new file with the same name
        fb.write_text("second")
        route_note(fb, sorted_, log_path=log_path, extract=make_extractor(cf=VALID_CF))
        assert (sorted_ / VALID_CF / "notes" / "collision_1.txt").exists(), "4: collision not renamed to _1"

        # 4b. collision-renamed note's json follows the renamed stem
        assert (sorted_ / VALID_CF / "notes" / "collision_1.json").exists(), "4b: collision json not renamed to _1"

        # 5. symlink source → needs_review with the symlink category in log
        real = root / "real.txt"
        real.write_text("real content")
        link = root / "symlink_note.txt"
        link.symlink_to(real)
        route_note(link, sorted_, log_path=log_path)
        assert (sorted_ / "needs_review" / "symlink_note.txt").exists(), "5: symlink not in needs_review"
        with open(log_path) as f:
            lines = f.readlines()
        assert any(REASON_SYMLINK in l for l in lines), "5: the symlink category not logged"

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
        dest9 = route_file(f9, sorted_, log_path)
        assert (sorted_ / VALID_CF / "records" / f9.name).exists(), "9: xlsx+CF not in patient records"
        assert dest9 == sorted_ / VALID_CF / "records" / f9.name, "9: route_file did not return xlsx+cf dest"

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

        # 14. a symlinked non-note input is skipped, not followed and filed by type/CF
        real_x = root / "real.xlsx"
        real_x.write_bytes(b"PK")
        link_x = root / f"fattura_{VALID_CF}_2026_link.xlsx"
        link_x.symlink_to(real_x)
        route_file(link_x, sorted_, log_path)
        assert (sorted_ / "needs_review" / link_x.name).exists(), "14: symlinked xlsx not in needs_review"
        assert not (sorted_ / VALID_CF / "records" / link_x.name).exists(), \
            "14: symlinked xlsx wrongly filed into patient records"

        # 15. pdf with CF in filename → sorted/<CF>/documents/
        f15 = root / f"referto_{VALID_CF}.pdf"
        f15.write_bytes(b"%PDF")
        dest15 = route_file(f15, sorted_, log_path)
        assert (sorted_ / VALID_CF / "documents" / f15.name).exists(), "15: pdf+CF not in patient documents"
        assert dest15 == sorted_ / VALID_CF / "documents" / f15.name, "15: route_file did not return pdf+cf dest"

        # 16. pdf without CF → sorted/documents/
        f16 = root / "referto_generico.pdf"
        f16.write_bytes(b"%PDF")
        dest16 = route_file(f16, sorted_, log_path)
        assert (sorted_ / "documents" / "referto_generico.pdf").exists(), "16: pdf no-cf not in top-level documents"
        assert dest16 == sorted_ / "documents" / "referto_generico.pdf", "16: route_file did not return pdf no-cf dest"

        # 17. route_note returns the matched-CF dest Path (return-value contract for the worker's extract= seam)
        f17 = root / "note17.txt"
        f17.write_text("patient note")
        dest17 = route_note(f17, sorted_, log_path=log_path, extract=make_extractor(cf=VALID_CF))
        assert dest17 == sorted_ / VALID_CF / "notes" / f17.name, "17: route_note did not return matched-CF dest"

        # 18. route_file forwards its extract= seam to route_note, not just accepting it
        f18 = root / "note18.txt"
        f18.write_text("patient note")
        dest18 = route_file(f18, sorted_, log_path, extract=make_extractor(cf=VALID_CF))
        assert dest18 == sorted_ / VALID_CF / "notes" / f18.name, "18: route_file did not forward extract to route_note"
        assert dest18.with_suffix(".json").exists(), "18: route_file's extract seam did not reach route_note's json write"

        # 19. the reasoned variants return a constant from the closed vocabulary,
        # never interpolated exception text. this is a privacy guard, not style:
        # dental_notes_schema raises "got {v!r}" carrying the codice fiscale,
        # and this value reaches audit_log and the staff UI.
        f19a = root / "note19a.txt"
        f19a.write_text("patient note")
        _, r19a = route_note_reasoned(
            f19a, sorted_, log_path=log_path,
            extract=make_extractor(error=ValueError(
                f"model output failed schema validation: codice_fiscale must match, got {VALID_CF!r}")),
        )
        assert r19a == REASON_EXTRACT_FAILED, "19: a rejected extraction should report REASON_EXTRACT_FAILED"
        assert VALID_CF not in r19a, "19: the reason must not carry the codice fiscale from the exception"
        assert "ValueError" not in r19a, "19: the reason must not carry exception type text"
        assert "schema validation" not in r19a, "19: the reason must not carry the exception message"

        f19b = root / "note19b.txt"
        f19b.write_text("patient note")
        _, r19b = route_note_reasoned(
            f19b, sorted_, log_path=log_path,
            extract=make_extractor(error=OllamaUnreachable("offline")),
        )
        assert r19b == REASON_MODEL_UNREACHABLE, "19: an unreachable model should report REASON_MODEL_UNREACHABLE"

        f19c = root / "note19c.txt"
        f19c.write_text("patient note")
        _, r19c = route_note_reasoned(
            f19c, sorted_, log_path=log_path, extract=make_extractor(cf=VALID_CF))
        assert r19c is None, "19: a successful route should report no reason"

        f19d = root / "archive19d.zip"
        f19d.write_text("not a real zip")
        _, r19d = route_file_reasoned(f19d, sorted_, log_path)
        assert r19d == REASON_UNSUPPORTED_TYPE, "19: an unknown type should report REASON_UNSUPPORTED_TYPE"

        link19 = root / "link19.txt"
        link19.symlink_to(root / "note19a.txt")
        _, r19e = route_file_reasoned(link19, sorted_, log_path)
        assert r19e == REASON_SYMLINK, "19: a symlink should report REASON_SYMLINK"

        # 19b. the same guard, but for what reaches DISK. phase 23 proved the
        # RETURNED reason is clean; this proves log.txt is too. the exception
        # here carries a codice fiscale exactly the way dental_notes_schema's
        # "got {v!r}" does.
        log19 = str(root / "leak_check.log")
        f19f = root / "note19f.txt"
        f19f.write_text("patient note")
        route_note_reasoned(
            f19f, sorted_, log_path=log19,
            extract=make_extractor(error=ValueError(
                f"model output failed schema validation: codice_fiscale must match, got {VALID_CF!r}")),
        )
        with open(log19) as f:
            written = f.read()
        assert VALID_CF not in written, \
            "19b: a codice fiscale from exception text must never reach log.txt"
        assert "schema validation" not in written, \
            "19b: exception text must not reach log.txt"
        assert REASON_EXTRACT_FAILED in written, \
            "19b: the categorical reason must still be logged"
        assert "note19f.txt" in written, \
            "19b: the log must still record which file was routed"

        # every reason is drawn from the closed set or is None - a future branch
        # cannot quietly introduce a fifth free-text reason
        for r in (r19a, r19b, r19c, r19d, r19e):
            assert r is None or r in NEEDS_REVIEW_REASONS, \
                f"19: reason {r!r} is not in the closed vocabulary"

    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    if len(sys.argv) < 2:
        print("usage: python sort_files.py <file|dir> [sorted_root]  |  python sort_files.py --selftest")
        sys.exit(1)
    src = Path(sys.argv[1])
    sorted_root = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("sorted")
    if src.is_dir():
        for f in src.rglob("*"):
            if f.is_file():
                route_file(f, sorted_root)
    else:
        route_file(src, sorted_root)


if __name__ == "__main__":
    main()
