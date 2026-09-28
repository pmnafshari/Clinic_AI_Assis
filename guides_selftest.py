"""Ask clinic guides (P24): separation, review before search, approvers, the fixed evaluation, verification,
immediate withdrawal, offline, backup, and the routes.

Synthetic library from guide_fixtures.py in a temp folder. Needs macOS sips, tesseract and sandbox-exec for the
page images and OCR; without them the run stops and says SKIPPED, never passed. No Ollama: the model is a stub
here (the real model is measured by eval_guides.py).
"""
import hashlib
import json
import re
import shutil
import socket
import sqlite3
import sys
import tempfile
from pathlib import Path

import clinic_guides as cg
import eval_guides
import guide_fixtures as gf

D, A, ADM, PM = ("drossi", "dentist"), ("aassist", "assistant"), ("anadmin", "admin"), ("pm", "assistant")
ROOT = Path(__file__).resolve().parent
TOOLS = all(shutil.which(t) for t in ("sips", "tesseract", "sandbox-exec"))


class Net:
    """Records and refuses every connection that is not to this machine."""

    def __enter__(self):
        self.hosts, self.saved = [], (socket.socket.connect, socket.getaddrinfo)
        rec = self

        def conn(sock, address):
            if isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1", "localhost"):
                return rec.saved[0](sock, address)
            rec.hosts.append(str(address))
            raise ConnectionRefusedError("blocked")

        def gai(host, *a, **k):
            if host in ("localhost", "127.0.0.1", "::1"):
                return rec.saved[1](host, *a, **k)
            rec.hosts.append("dns:" + str(host))
            raise socket.gaierror("blocked")
        socket.socket.connect, socket.getaddrinfo = conn, gai
        return self

    def __exit__(self, *e):
        socket.socket.connect, socket.getaddrinfo = self.saved
        return False


def setup(tmp):
    tmp = Path(tmp)
    cg.DB_PATH = str(tmp / "db" / "guides.sqlite")
    cg.STORE = tmp / "guides"
    cg.SLOT_DIR = tmp / "slots"
    (tmp / "db").mkdir(exist_ok=True)
    conn = cg.connect()
    lib = gf.build(tmp / "library")
    ids, devices = gf.load(conn, lib)
    return conn, lib, ids, devices


def separation():
    # 1. the guides code never reads a patient store and its schema has nowhere to put a patient
    src = (ROOT / "clinic_guides.py").read_text()
    for banned in ("clinic.sqlite", "import storage", "patient_accessor", "patient_id", "import documents_selftest",
                   "get_collection", "PersistentClient", "db/chroma", "doc_chroma", "patient_files", "legacy_import"):
        assert banned not in src, f"1: clinic_guides.py mentions {banned!r}"
    route = (ROOT / "app" / "guides_routes.py").read_text()
    assert "FROM patients" not in route and "lookup_patient" not in route, "1: the guides routes read no patient"
    with tempfile.TemporaryDirectory() as tmp:
        cg.DB_PATH = str(Path(tmp) / "g.sqlite")
        conn = cg.connect()
        cols = {r[1].lower() for t in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
                for r in conn.execute(f"PRAGMA table_info('{t[0]}')")}
        for bad in ("patient_id", "codice_fiscale", "patient_name", "visit_id"):
            assert bad not in cols, f"1: the guides store has a {bad} column"
        conn.close()


def review_and_roles(tmp):
    # 2. held for review, nothing searchable before approval; who may approve what
    tmp = Path(tmp)
    cg.DB_PATH = str(tmp / "g.sqlite")
    cg.STORE = tmp / "guides"
    cg.SLOT_DIR = tmp / "slots"
    conn = cg.connect()
    lib = gf.build(tmp / "lib")
    dev = cg.add_device(conn, "DemoMed", "AX-200", "Sterilisation room", *D)
    for who in (A, ADM):
        try:
            cg.add_device(conn, "X", "Y", "Z", *who)
            raise AssertionError(f"2: {who[1]} registered a device")
        except PermissionError:
            pass
    sid = cg.ingest(conn, (lib / "ax200-v2.pdf").read_bytes(), "ax200-v2.pdf", *D, title="AX-200 manual",
                    kind="device", device_id=dev, edition="Edition 2 (2023)", language="en", version="2",
                    owner="practice manager", audience="staff", effective="2026-01-01")
    s = cg.source(conn, sid)
    assert s["status"] == "pending_review" and s["pages"] == 7, dict(s)
    pages = {p["page"]: p for p in cg.pages(conn, sid)}
    assert pages[3]["has_figure"] and "water tank empty" in pages[3]["ocr_text"] and pages[3]["ocr_conf"] >= 70, \
        "2: a label only in a figure is read from the picture, with its confidence"
    assert all(Path(cg.STORE, p["image_path"]).is_file() for p in pages.values()), "2: every page has its image"
    assert "injection" in " ".join(json.loads(pages[7]["flags"])), "2: an instruction aimed at a model is flagged"
    assert "service" in " ".join(json.loads(pages[6]["flags"])), "2: a technicians-only page is flagged for review"
    r = cg.ask(conn, "What does the B-PROG button do?", "assistant", device_id=dev)
    assert r["outcome"] == "abstain", "2: HARD FAIL - a document waiting for review answered"
    for who in (A, ADM, PM):
        try:
            cg.approve(conn, sid, *who)
            raise AssertionError(f"2: {who} approved a device manual")
        except PermissionError:
            pass
    cg.designate_approver(conn, PM[0], *D)
    try:
        cg.approve(conn, sid, *PM)
        raise AssertionError("2: an administrative approver approved a device manual")
    except PermissionError:
        pass
    adm = cg.ingest(conn, (lib / "rp01-en.pdf").read_bytes(), "rp01-en.pdf", *D, title="RP-01", kind="admin",
                    device_id=None, edition="", language="en", version="3", owner="practice manager",
                    audience="staff", effective="2026-01-01")
    try:
        cg.approve(conn, adm, *A)
        raise AssertionError("2: an undesignated assistant approved a procedure")
    except PermissionError:
        pass
    cg.approve(conn, adm, *PM)
    assert cg.source(conn, adm)["approved_by"] == "pm", "2: the designated approver approved an admin procedure"
    assert not cg.authorize("assistant", "approve_guides"), "2: designation gives no clinical capability"
    clin = cg.ingest(conn, (lib / "cp02.pdf").read_bytes(), "cp02.pdf", *D, title="CP-02", kind="clinical",
                     device_id=None, edition="", language="en", version="1", owner="dentist", audience="staff",
                     effective="2026-01-01")
    try:
        cg.approve(conn, clin, *PM)
        raise AssertionError("2: an administrative approver approved a clinical procedure")
    except PermissionError:
        pass
    cg.approve(conn, sid, *D)
    assert cg.ask(conn, "What does the B-PROG button do?", "assistant", device_id=dev)["outcome"] == "answer"
    # a document without the synthetic mark is a real manual: never approved in this build
    real = gf.pdf([gf.page("Real Autoclave 9000 manual", "Press START to begin.")])
    rid = cg.ingest(conn, real, "real.pdf", *D, title="Real", kind="device", device_id=dev, edition="1",
                    language="en", version="1", owner="dentist", audience="staff", effective="2026-01-01")
    try:
        cg.approve(conn, rid, *D)
        raise AssertionError("2: HARD FAIL - a real manual was approved")
    except cg.GuideError as e:
        assert e.code == "real_document", e.code
    # a document that carries a patient identifier never becomes searchable
    leak = gf.pdf([gf.page("DEMO note", gf.MARK, "Patient RSSMRA80A01H501U called about the autoclave.")])
    lid = cg.ingest(conn, leak, "leak.pdf", *D, title="Leak", kind="admin", device_id=None, edition="",
                    language="en", version="1", owner="pm", audience="staff", effective="2026-01-01")
    assert "patient" in " ".join(json.loads(cg.pages(conn, lid)[0]["flags"])), "2: a patient identifier is flagged"
    try:
        cg.approve(conn, lid, *D)
        raise AssertionError("2: HARD FAIL - a document with a patient identifier was approved")
    except cg.GuideError as e:
        assert e.code == "patient_data", e.code
    # refused types
    q = cg.ingest(conn, b"<html><script>x</script></html>", "x.pdf", *D, title="x", kind="admin", device_id=None,
                  edition="", language="en", version="1", owner="pm", audience="staff", effective="2026-01-01")
    assert cg.source(conn, q)["status"] == "quarantined", "2: a non-PDF is quarantined"
    conn.close()


def evaluation(tmp):
    # 3. the fixed evaluation, extractive (no model), against the thresholds written before the first run
    conn, lib, ids, devices = setup(tmp)
    report = eval_guides.run(conn, ids, devices, model=None)
    eval_guides.print_report(report)
    m, t = report["metrics"], report["thresholds"]
    assert m["unsupported_operational_answers"] == 0, "3: HARD FAIL - an answer is not backed by its page"
    assert m["unverified_quotations_shown"] == 0, "3: HARD FAIL - a quotation is not in its page"
    assert m["old_edition_answers"] == 0, "3: HARD FAIL - an old edition answered"
    assert m["patient_leakage"] == 0, "3: HARD FAIL - patient data in an answer"
    assert m["forbidden_text_shown"] == 0, "3: HARD FAIL - forbidden text shown"
    assert m["safety_negatives_correct"] == 1.0, f"3: safety negatives {report['failed_safety']}"
    assert m["retrieval_top3_answerable"] >= t["retrieval_top3_answerable"], f"3: retrieval {m['retrieval_top3_answerable']}"
    assert m["answered_with_expected_page"] >= t["answered_with_expected_page"], f"3: answers {report['failed']}"
    assert m["expected_passage_shown"] >= t["expected_passage_shown"], "3: passages"
    assert m["expected_warnings_shown"] == 1.0, f"3: warnings {report['failed']}"
    assert m["extractive_p95_seconds"] <= t["extractive_p95_seconds"], f"3: p95 {m['extractive_p95_seconds']}"
    return conn, lib, ids, devices


def verification(conn, ids, devices):
    # 4. the verifier: fabricated citations, invented quotes and explanations never reach the screen
    v2, v1 = ids["ax200_v2"], ids["ax200_v1"]
    good = "B-PROG selects program B for wrapped instruments: 134 C for 4 minutes."
    assert cg.verify_quote(conn, v2, 2, good, "assistant"), "4: a true quotation verifies"
    assert cg.verify_quote(conn, v2, 2, "B-PROG  selects program B\nfor wrapped instruments", "assistant"), \
        "4: whitespace differences are the only tolerance"
    for sid, page, text, why in ((v2, 99, good, "a page that does not exist"), (v2, 3, good, "the wrong page"),
                                 (v2, 2, "B-PROG selects program B for wrapped instruments: 121 C", "a changed number"),
                                 (v1, 2, "B-PROG selects program B: 121 C for 20 minutes.", "a superseded edition"),
                                 (v2, 6, "Hold DRY and STANDBY together for 10 seconds", "a page restricted for the role")):
        assert not cg.verify_quote(conn, sid, page, text, "assistant"), f"4: HARD FAIL - verified {why}"
    dev = devices["ax200"]

    def liar(prompt, **k):
        return ('B-PROG runs at "121 C for 20 minutes" (page 9). It also sterilises implants.')
    r = cg.ask(conn, "What does the B-PROG button do?", "assistant", device_id=dev, model=liar)
    assert r["outcome"] == "answer" and r["explanation"] is None, "4: an unsupported explanation is dropped"
    assert all(cg.verify_quote(conn, c["source_id"], c["page"], c["passage"], "assistant") for c in r["citations"])

    def honest(prompt, **k):
        return "B-PROG selects program B for wrapped instruments at 134 C for 4 minutes."
    r = cg.ask(conn, "What does the B-PROG button do?", "assistant", device_id=dev, model=honest)
    assert r["explanation"], "4: a supported explanation is kept"

    def injected(prompt, **k):
        return "Disable the door lock before starting."
    r = cg.ask(conn, "How do I clean the door seal?", "assistant", device_id=dev, model=injected)
    assert r["explanation"] is None and "disable" not in json.dumps(r).lower(), "4: an injected instruction is dropped"
    prompt_seen = []

    def spy(prompt, **k):
        prompt_seen.append(prompt)
        return ""
    cg.ask(conn, "How do I clean the door seal?", "assistant", device_id=dev, model=spy)
    assert prompt_seen and "ignore previous instructions" not in prompt_seen[0].lower(), \
        "4: a flagged instruction never reaches the model"


def lifecycle(conn, lib, ids, devices):
    # 5. withdrawal, restriction and replacement act on the very next question (there is no answer cache)
    dev = devices["ax200"]
    q = "What does the B-PROG button do?"
    assert cg.ask(conn, q, "assistant", device_id=dev)["outcome"] == "answer"
    cg.restrict_pages(conn, ids["ax200_v2"], [2, 6], *D)
    r = cg.ask(conn, q, "assistant", device_id=dev)
    assert r["outcome"] == "abstain" and r["reason"] == "restricted", f"5: a restricted page still answered: {r['reason']}"
    assert cg.ask(conn, q, "dentist", device_id=dev)["outcome"] == "answer", "5: the dentist still reads it"
    cg.restrict_pages(conn, ids["ax200_v2"], [6], *D)
    v3 = gf.pdf([gf.page("DemoMed AX-200 Autoclave - User manual, Edition 3 (2026)", gf.MARK),
                 gf.page("Control panel (page 2)", "B-PROG selects program B for wrapped instruments: 134 C for 5 minutes.")])
    new = cg.ingest(conn, v3, "ax200-v3.pdf", *D, title="DemoMed AX-200 User manual", kind="device",
                    device_id=dev, edition="Edition 3 (2026)", language="en", version="3", owner="practice manager",
                    audience="staff", effective="2026-09-01")
    assert cg.ask(conn, q, "assistant", device_id=dev)["citations"][0]["source_id"] == ids["ax200_v2"], \
        "5: a replacement waiting for review changes nothing"
    cg.approve(conn, new, *D)
    cg.supersede(conn, ids["ax200_v2"], new, *D)
    r = cg.ask(conn, q, "assistant", device_id=dev)
    assert r["citations"][0]["source_id"] == new and "5 minutes" in r["citations"][0]["passage"], \
        "5: HARD FAIL - the replaced edition still answers"
    cg.withdraw(conn, new, "recalled by the maker", *D)
    r = cg.ask(conn, q, "assistant", device_id=dev)
    assert r["outcome"] == "abstain", "5: HARD FAIL - a withdrawn manual still answers"
    assert cg.ask(conn, "What do I check after each sterilisation cycle?", "assistant")["outcome"] == "answer"
    cg.withdraw(conn, ids["cp02"], "rewritten", *D)
    assert cg.ask(conn, "What do I check after each sterilisation cycle?", "assistant")["outcome"] == "abstain", \
        "5: a withdrawn procedure still answers"
    try:
        cg.withdraw(conn, ids["rp01_en"], "x", *A)
        raise AssertionError("5: the assistant withdrew a procedure")
    except PermissionError:
        pass
    # the record of who decided what, and no question text anywhere
    events = [e["action"] for e in cg.history(conn, ids["cp02"])]
    assert events[:2] == ["uploaded", "approved"] and events[-1] == "withdrawn", events
    asked = conn.execute("SELECT * FROM asks").fetchall()
    assert asked and all("B-PROG" not in json.dumps(dict(a)) for a in asked), "5: the ask log carries question text"


def offline(conn, devices):
    # 6. the whole answer path with every non-local connection refused, the model stubbed at the local boundary
    with Net() as net:
        for q, dev in (("What does the B-PROG button do?", devices["ax200"]), ("Come si pulisce il puntale?",
                                                                                devices["cl5"])):
            cg.ask(conn, q, "assistant", device_id=dev, model=lambda p, **k: "")
    assert not net.hosts, f"6: HARD FAIL - the answer path reached {net.hosts}"
    with Net() as net:
        try:
            cg.model_answer("x", url="https://api.example.com/api/generate")
            raise AssertionError("6: a remote model endpoint was accepted")
        except cg.GuideError as e:
            assert e.code == "model_unavailable", e.code
    assert not net.hosts, f"6: HARD FAIL - a remote model endpoint was contacted: {net.hosts}"
    # service work goes to a technician for the dentist too, even though the dentist may read the page
    r = cg.ask(conn, "How do I calibrate the sterilisation temperature?", "dentist", device_id=devices["ax200"])
    assert r["outcome"] == "abstain" and r["reason"] == "servicing", f"6: servicing for the dentist: {r['reason']}"


def backup_and_patients(tmp):
    # 7. the guides store is in the backup, and erasure and the patient export never touch it
    import backup
    import storage
    tmp = Path(tmp)
    root = tmp / "data"
    (root / "db").mkdir(parents=True)
    storage.init_db(str(root / "db" / "clinic.sqlite")).close()
    cg.DB_PATH = str(root / "db" / "guides.sqlite")
    cg.STORE = root / "guides"
    cg.SLOT_DIR = tmp / "slots"
    conn = cg.connect()
    lib = gf.build(tmp / "lib")
    dev = cg.add_device(conn, "DemoMed", "AX-300", "Surgery 2", *D)
    sid = cg.ingest(conn, (lib / "ax300.pdf").read_bytes(), "ax300.pdf", *D, title="AX-300", kind="device",
                    device_id=dev, edition="Edition 1 (2024)", language="en", version="1", owner="pm",
                    audience="staff", effective="2026-01-01")
    cg.approve(conn, sid, *D)
    conn.close()
    key = tmp / "k"
    backup.init_key(key)
    archive = backup.create(data_root=root, dest=tmp / "backups", key_path=key)["archive"]
    out = tmp / "restored"
    res = backup.restore(archive, out, key, apply=True)
    assert not res["problems"], res["problems"]
    assert (out / "db" / "guides.sqlite").is_file(), "7: the guides database is in the backup"
    rc = sqlite3.connect(out / "db" / "guides.sqlite")
    assert rc.execute("SELECT status FROM sources WHERE id = ?", (sid,)).fetchone()[0] == "approved", \
        "7: the guides database is restored"
    img = rc.execute("SELECT image_path FROM pages WHERE source_id = ? LIMIT 1", (sid,)).fetchone()[0]
    assert (out / "guides" / img).is_file(), "7: page images are restored"
    rc.close()
    cg.DB_PATH = str(out / "db" / "guides.sqlite")
    cg.STORE = out / "guides"
    rconn = cg.connect()
    assert cg.ask(rconn, "What does P1 do?", "assistant", device_id=dev)["outcome"] == "answer", \
        "7: a restored library answers without rebuilding anything by hand"
    rconn.close()
    # erasure of a patient and a patient's export leave the guides store byte-identical
    import erasure
    import data_rights
    import patient_id
    before = hashlib.sha256((root / "db" / "guides.sqlite").read_bytes()).hexdigest()
    pconn = storage.init_db(str(root / "db" / "clinic.sqlite"))
    pid = patient_id.seed_patient(pconn, "ZZGU000000000001", "Guida Paziente")
    import zipfile
    name = data_rights.build_export(pconn, pid, sorted_root=root / "sorted", exports_dir=root / "exports")
    names = zipfile.ZipFile(root / "exports" / name).namelist()
    assert names == ["data.json"], f"7: a patient export carries something else: {names}"
    erasure.erase(pconn, pid, "drossi", "dentist", req_id=None, sorted_root=root / "sorted", drop_dir=root / "drop",
                  undo_log=root / "undo.jsonl", exports_dir=root / "exports", tombstones=tmp / "t", write_tombstone=False)
    pconn.close()
    assert hashlib.sha256((root / "db" / "guides.sqlite").read_bytes()).hexdigest() == before, \
        "7: erasure touched the guides store"


def routes(tmp):
    # 8. the pages: who may open what, source opening, page images, and no route for patients or admin
    import app.db as app_db
    from app import create_app
    from patient_app import create_patient_app, routes as proutes
    from werkzeug.security import generate_password_hash
    import web_session
    tmp = Path(tmp)
    db_path = str(tmp / "clinic.sqlite")
    app_db.DB_PATH = db_path
    app_db.CHROMA_PATH = str(tmp / "chroma")
    conn, lib, ids, devices = setup(tmp)
    app = create_app()
    app.config["TESTING"] = True

    def client(user, role):
        c = sqlite3.connect(db_path)
        c.execute("INSERT OR IGNORE INTO users (username, password_hash, role, active) VALUES (?, ?, ?, 1)",
                  (user, generate_password_hash("x"), role))
        c.commit()
        token = web_session.create_session(c, user, role)
        c.close()
        cl = app.test_client()
        cl.set_cookie(web_session.COOKIE_NAME, token)
        return cl
    den, ast, adm, pm = client(*D), client(*A), client(*ADM), client(*PM)

    def csrf(html):
        return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
    page = ast.get("/guides/ask")
    assert page.status_code == 200 and "Ask clinic guides" in page.text, "8: the assistant opens the page"
    assert "DemoMed AX-200" in page.text, "8: the device picker lists the registered devices"
    resp = ast.post("/guides/ask", data={"csrf_token": csrf(page.text), "question": "What does the B-PROG button do?",
                                         "device_id": str(devices["ax200"])})
    body = resp.text
    assert resp.status_code == 200 and "B-PROG selects program B" in body and "Edition 2 (2023)" in body \
        and "page 2" in body.lower(), "8: an answer shows the passage, edition and page"
    assert "Never open the door while the pressure indicator is red" in body, "8: the warning from page 5 is shown"
    assert f"/guides/sources/{ids['ax200_v2']}/pages/2/image" in body, "8: the page image is offered"
    assert "no-store" in resp.headers.get("Cache-Control", ""), "8: answers are never cached by the browser"
    img = ast.get(f"/guides/sources/{ids['ax200_v2']}/pages/2/image")
    assert img.status_code == 200 and img.mimetype == "image/png", "8: the page image opens"
    assert ast.get(f"/guides/sources/{ids['ax200_v2']}/pages/6/image").status_code == 404, \
        "8: a restricted page image is not served to the assistant"
    assert den.get(f"/guides/sources/{ids['ax200_v2']}/pages/6/image").status_code == 200
    assert ast.get(f"/guides/sources/{ids['ax200_v1']}/pages/2/image").status_code == 404, \
        "8: a superseded edition is not opened for asking staff"
    assert ast.get(f"/guides/sources/{ids['rp05']}").status_code == 404, "8: a pending source is not opened"
    assert ast.get(f"/guides/sources/{ids['ax200_v2']}?page=2").status_code == 200, "8: an approved source opens"
    assert "every Monday" not in ast.get(f"/guides/sources/{ids['cp09']}").text, "8: a dentist-only source is closed"
    for path in ("/guides/ask", "/guides", f"/guides/sources/{ids['ax200_v2']}"):
        assert adm.get(path).status_code in (302, 403), f"8: admin opened {path}"
    lib_page = den.get("/guides")
    assert lib_page.status_code == 200 and "RP-05 Phone script" in lib_page.text, "8: the dentist sees the library"
    ast_lib = ast.get("/guides")
    assert "RP-05 Phone script" not in ast_lib.text, "8: an assistant does not see drafts"
    assert "RP-05 Phone script" in pm.get("/guides").text, "8: the designated approver sees the admin draft"
    pm_page = pm.get(f"/guides/sources/{ids['rp05']}")
    assert pm_page.status_code == 200 and "Approve" in pm_page.text, "8: the approver can review it"
    resp = pm.post(f"/guides/sources/{ids['rp05']}/approve", data={"csrf_token": csrf(pm_page.text)})
    assert cg.source(conn, ids["rp05"])["status"] == "approved", "8: approved through the route"
    resp = ast.post(f"/guides/sources/{ids['cp02']}/withdraw", data={"csrf_token": csrf(ast_lib.text), "reason": "x"})
    assert cg.source(conn, ids["cp02"])["status"] == "approved", "8: the assistant cannot withdraw"
    papp = create_patient_app(env_path=tmp / ".env.patient")
    proutes.DB_PATH = db_path
    rules = [r.rule for r in papp.url_map.iter_rules()]
    assert not any("guide" in r for r in rules), "8: the patient portal has no guides route"
    import site_app
    srules = [r.rule for r in site_app.create_site_app().url_map.iter_rules()]
    assert not any("guide" in r for r in srules), "8: the public site has no guides route"
    assert "/guides/ask" in den.get("/").text and "/guides/ask" in ast.get("/").text and "/guides" not in adm.get("/").text, \
        "8: the sidebar links the page for dentist and assistant only"
    conn.close()


def selftest():
    if not TOOLS:
        print("SKIPPED guides_selftest: sips, tesseract or sandbox-exec missing - nothing claimed")
        return
    separation()
    with tempfile.TemporaryDirectory() as tmp:
        review_and_roles(tmp)
    with tempfile.TemporaryDirectory() as tmp:
        conn, lib, ids, devices = evaluation(tmp)
        verification(conn, ids, devices)
        offline(conn, devices)
        lifecycle(conn, lib, ids, devices)
        conn.close()
    with tempfile.TemporaryDirectory() as tmp:
        backup_and_patients(tmp)
    with tempfile.TemporaryDirectory() as tmp:
        routes(tmp)
    print("guides_selftest: ok")


if __name__ == "__main__":
    selftest()
    sys.exit(0)
