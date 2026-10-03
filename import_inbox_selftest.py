"""The browser import of a synthetic folder (UIF): inbox -> check -> stage -> confirm -> the record.

Staff app over a temp database, a temp import inbox and a temp staging folder. The folder is the Drive test
set (legacy_fixtures.build_drive, D01-D05) with expectations frozen in .planning/plans/UIF.md before the first
run. Nothing here needs a model, a server or the network.
"""
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

import app.db as app_db
import documents as docs
import legacy_fixtures as fx
import legacy_import as li
import patient_id
from patient_files_selftest import csrf, staff_client

D, A, ADM = ("drossi", "dentist"), ("aassist", "assistant"), ("anadmin", "admin")
FOLDER = "clinic-share-folder"
EXPECTED = {"D01": ("proposed", "strong", "lorenzo"), "D02": ("conflict", None, None),
            "D03": ("unmatched", None, None), "D04": ("unmatched", None, None),
            "D05": ("proposed", "strong", "lorenzo")}


def snapshot(folder):
    return {p.name: (p.read_bytes(), p.stat().st_size, p.stat().st_mtime_ns)
            for p in sorted(Path(folder).iterdir()) if p.is_file()}


def counts(conn):
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("import_batches", "import_items", "patient_documents", "document_publications")}


def staged(root):
    root = Path(root)
    return sorted(p.name for p in root.iterdir()) if root.is_dir() else []


def service(tmp, conn, pids):
    inbox = Path(li.INBOX)
    # 1. the inbox lists its direct folders by name; links and hidden folders are not offered
    assert li.inbox_folders() == [], "1: an empty or missing inbox lists nothing"
    cases = fx.build_drive(inbox / FOLDER, conn)
    (inbox / "plain").mkdir()
    (inbox / "plain" / "a.txt").write_text("x")
    (inbox / ".hidden").mkdir()
    os.symlink(inbox / FOLDER, inbox / "linked")
    (inbox / "loose.txt").write_text("a file, not a folder")
    listed = li.inbox_folders()
    assert [f["name"] for f in listed] == [FOLDER, "plain"], f"1: listed {listed}"
    assert [f["synthetic"] for f in listed] == [True, False], "1: only the marked folder is a synthetic test folder"
    # 2. a name is accepted only if the inbox lists it: no path escapes, no links
    assert li.inbox_folder(FOLDER) == inbox / FOLDER
    for bad in ("..", "../" + FOLDER, "linked", ".hidden", "loose.txt", "nope", "", FOLDER + "/x"):
        try:
            li.inbox_folder(bad)
            raise AssertionError(f"2: HARD FAIL - {bad!r} was accepted as an inbox folder")
        except li.ImportProblem as e:
            assert e.code == "no_folder", e.code
    # 3. the marker guard is unchanged: an unmarked inbox folder is never read
    for fn in (lambda: li.dry_run(conn, li.inbox_folder("plain")),
               lambda: li.stage(conn, li.inbox_folder("plain"), *D)):
        try:
            fn()
            raise AssertionError("3: HARD FAIL - an unmarked folder was read")
        except li.ImportProblem as e:
            assert e.code == "not_authorised", e.code
    return cases


def routes(tmp, conn, pids, cases, app, db_path):
    inbox = Path(li.INBOX)
    dentist = staff_client(app, db_path, *D)
    assistant = staff_client(app, db_path, *A)
    admin = staff_client(app, db_path, *ADM)
    before_src = snapshot(inbox / FOLDER)
    before = counts(conn)

    # 4. the dentist sees the inbox with its two actions; the unmarked folder has none
    page = dentist.get("/imports").text
    assert "Import a synthetic test folder" in page, "4: the dentist is offered the browser import"
    assert FOLDER in page and "Check this folder" in page and "Stage and propose" in page, "4: both actions"
    assert re.search(r"plain</[^>]+>.*?not a synthetic test folder", page, re.S), \
        "4: an unmarked folder is listed as not importable"
    assert page.count('name="folder" value="plain"') == 0, "4: no action is offered for the unmarked folder"
    assert "linked" not in page and ".hidden" not in page, "4: links and hidden folders are not listed"
    assert "legacy_import.py" not in page, "4: the page no longer sends the dentist to a command line"

    # 5. reception and admin: no control, and the actions refuse server-side
    ap = assistant.get("/imports").text
    assert "Check this folder" not in ap and "Stage and propose" not in ap and FOLDER not in ap, \
        "5: the assistant gets progress only, no folder names or actions"
    for client in (assistant, admin):
        token = csrf(client.get("/", follow_redirects=True).text)
        for action in ("check", "stage"):
            r = client.post(f"/imports/inbox/{action}", data={"csrf_token": token, "folder": FOLDER})
            assert r.status_code == 302, f"5: {action} not refused for a non-dentist ({r.status_code})"
    assert counts(conn) == before and staged(li.STAGING_ROOT) == [], "5: HARD FAIL - a refused action wrote something"
    refusals = conn.execute("SELECT COUNT(*) FROM audit_log WHERE action IN ('import_check', 'import_stage')"
                            " AND allowed = 0").fetchone()[0]
    assert refusals == 4, f"5: each refusal is audited ({refusals} of 4)"

    # 6. csrf: a post without the token changes nothing
    r = dentist.post("/imports/inbox/stage", data={"folder": FOLDER})
    assert r.status_code == 400 and counts(conn) == before, "6: a stage without csrf is refused"

    # 7. check = the dry run in the browser: every file and its result, nothing written
    r = dentist.post("/imports/inbox/check", data={"csrf_token": csrf(page), "folder": FOLDER})
    check = r.text
    assert r.status_code == 200 and "nothing was written" in check, "7: the check says it wrote nothing"
    for case, (name, _expected) in cases.items():
        assert name in check, f"7: {case} {name} is in the check"
    table = check.split('id="check"', 1)[1].split("</section>", 1)[0]
    shown = {"D01": "Proposed (strong)", "D02": "Conflict", "D03": "Unmatched", "D04": "Unmatched",
             "D05": "Proposed (strong)"}
    for case, (name, _expected) in cases.items():
        row = next((r for r in table.split("<tr>") if name in r), "")
        assert shown[case] in row, f"7: {case}'s row does not show its result ({shown[case]})"
        assert ("Lorenzo Bruno" in row) == (case in ("D01", "D05")), f"7: {case}'s row names the wrong owner"
    assert counts(conn) == before and staged(li.STAGING_ROOT) == [], "7: HARD FAIL - the check wrote something"
    for bad in ("plain", "../" + FOLDER, "linked", "nope"):
        r = dentist.post("/imports/inbox/check", data={"csrf_token": csrf(page), "folder": bad})
        assert r.status_code in (302, 404) or "not a synthetic test folder" in r.text, f"7: {bad!r} was checked"
        assert counts(conn) == before and staged(li.STAGING_ROOT) == [], f"7: HARD FAIL - {bad!r} wrote something"

    # 8. stage: proposals from evidence, and still zero files attached to anyone
    r = dentist.post("/imports/inbox/stage", data={"csrf_token": csrf(page), "folder": FOLDER})
    assert r.status_code == 302 and "/imports" in r.headers["Location"], "8: staged, back to the queue"
    after = counts(conn)
    assert after["import_batches"] == before["import_batches"] + 1 and after["import_items"] == len(cases), \
        f"8: one run, one item per file ({after})"
    assert after["patient_documents"] == before["patient_documents"] and after["document_publications"] == 0, \
        "8: HARD FAIL - staging attached or published a file"
    rows = {Path(r["rel_path"]).name: r for r in li.items(conn, None)}
    first_run = []
    for case, (name, _expected) in sorted(cases.items()):
        state, strength, who = EXPECTED[case]
        got = rows[name]
        first_run.append(f"{case} {name}: {got['state']} {got['strength']} "
                         f"{'candidate' if got['patient_id'] else 'no candidate'}")
        assert got["state"] == state and got["strength"] == strength, \
            f"8: {case} expected {state}/{strength}, got {got['state']}/{got['strength']}"
        assert got["patient_id"] == (pids[who] if who else None), f"8: {case} names the wrong patient"
    for pid in pids.values():
        n = conn.execute("SELECT COUNT(*) FROM patient_documents WHERE patient_id = ?", (pid,)).fetchone()[0]
        assert n == 0, "8: HARD FAIL - a strong proposal attached a file before any confirmation"
    leads = li.evidence(rows["D03-lettera-santoro.txt"])
    assert rows["D03-lettera-santoro.txt"]["patient_id"] is None and leads["candidates"] in ([], [pids["giulia"]]), \
        "8: a name and a booked time stay leads"
    queue = dentist.get("/imports").text
    assert "D01-referto-bruno.pdf" in queue and "proposed: Lorenzo Bruno" in queue, "8: the queue shows the proposal"

    # 9. a second click, or the same folder again, is the same import
    r = dentist.post("/imports/inbox/stage", data={"csrf_token": csrf(page), "folder": FOLDER})
    assert counts(conn) == after, f"9: HARD FAIL - restaging an unchanged folder added rows ({counts(conn)})"

    # 10. the source folder is untouched by the check and the stage
    assert snapshot(inbox / FOLDER) == before_src, "10: HARD FAIL - the import changed a source file"

    # 11. confirmation is the only way in: D01 into Lorenzo's record, D05 (same bytes) the same document
    lor = pids["lorenzo"]
    cf = conn.execute("SELECT codice_fiscale FROM patients WHERE patient_id = ?", (lor,)).fetchone()[0]
    for name in ("D01-referto-bruno.pdf", "D05-referto-bruno-copia.pdf"):
        it = rows[name]
        page = dentist.get(f"/imports/items/{it['id']}").text
        r = dentist.post(f"/imports/items/{it['id']}/confirm",
                         data={"csrf_token": csrf(page), "patient_cf": cf, "expected_sha": it["sha256"],
                               "reason": "", "visit_id": ""})
        assert r.status_code == 302 and li.item(conn, it["id"])["state"] == "confirmed", f"11: {name} confirmed"
    docs_for = conn.execute("SELECT id FROM patient_documents WHERE patient_id = ?", (lor,)).fetchall()
    assert len(docs_for) == 1, f"11: two copies of the same bytes made {len(docs_for)} documents"
    others = conn.execute("SELECT COUNT(*) FROM patient_documents WHERE patient_id != ?", (lor,)).fetchone()[0]
    assert others == 0, "11: HARD FAIL - a file reached another patient"
    record = dentist.get(f"/patients/{cf}").text
    assert "D01-referto-bruno.pdf" in record or "referto" in record.lower(), "11: the file is on the dentist's record"
    assert conn.execute("SELECT COUNT(*) FROM document_publications").fetchone()[0] == 0, \
        "11: HARD FAIL - confirmation published the file to the portal"
    for name in ("D02-referto-due-codici.txt", "D03-lettera-santoro.txt"):
        assert li.item(conn, rows[name]["id"])["state"] in ("conflict", "unmatched"), f"11: {name} still waits"

    # 12. only an UNCHANGED folder is the same import: a new file in it is staged, nothing old twice
    (inbox / FOLDER / "D06-nuovo.txt").write_bytes(b"Nota DEMO senza dati\n")
    held = counts(conn)
    r = dentist.post("/imports/inbox/stage", data={"csrf_token": csrf(page), "folder": FOLDER})
    now = counts(conn)
    assert now["import_batches"] == held["import_batches"] + 1 and now["import_items"] == held["import_items"] + 1, \
        f"12: a new file in the folder was not staged ({held} -> {now})"
    assert now["patient_documents"] == held["patient_documents"], "12: HARD FAIL - restaging attached a file"
    return first_run


def selftest():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        db_path = str(tmp / "clinic.sqlite")
        app_db.DB_PATH = db_path
        app_db.CHROMA_PATH = str(tmp / "chroma")
        docs.DOC_ROOT = tmp / "documents"
        docs.DOC_CHROMA_PATH = str(tmp / "doc_chroma")
        docs.SLOT_DIR = tmp / "slots"
        docs.unindex = lambda ids: None
        li.STAGING_ROOT = tmp / "import_staging"
        li.INBOX = tmp / "import_inbox"
        from app import create_app
        app = create_app()
        app.config["TESTING"] = True
        from storage import connect
        conn = connect(db_path)
        conn.row_factory = sqlite3.Row
        pids = {k: patient_id.seed_patient(conn, code, name, None) for k, (code, name) in fx.DRIVE_PEOPLE.items()}
        conn.commit()
        cases = service(tmp, conn, pids)
        first_run = routes(tmp, conn, pids, cases, app, db_path)
        conn.close()
    if "-v" in sys.argv:
        print("\n".join(first_run))
    print("import_inbox_selftest: ok")


if __name__ == "__main__":
    selftest()
    sys.exit(0)
