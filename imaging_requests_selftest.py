"""P26 demo imaging requests and the reception handoff: the locked acceptance cases C01-C20.

Synthetic patients, requests, guides and files in a temp folder. The guidance cases need the P24 synthetic library
(macOS sips, tesseract, sandbox-exec); without them those checks say SKIPPED, never passed.
"""
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path

import clinic_guides as cg
import clinic_time
import documents as docs
import guide_fixtures as gf
import imaging_requests as ir
import patient_files as pf
import patient_id

D, D2, A, ADM = ("drossi", "dentist"), ("dbianchi", "dentist"), ("aassist", "assistant"), ("anadmin", "admin")
T0 = clinic_time.read_instant("2026-09-28T08:00:00+00:00")
TOOLS = all(shutil.which(t) for t in ("sips", "tesseract", "sandbox-exec"))
CF = {"paola_a": "ZZIP000000000001", "paola_b": "ZZIP000000000002", "mario": "ZZIM000000000003",
      "vera": "ZZIV000000000004"}


def setup(tmp, guides=True):
    from storage import init_db
    tmp = Path(tmp)
    docs.DOC_ROOT = tmp / "documents"
    docs.DOC_CHROMA_PATH = str(tmp / "doc_chroma")
    docs.SLOT_DIR = tmp / "slots"
    docs.unindex = lambda ids: None
    docs._index = lambda r: None
    cg.DB_PATH = str(tmp / "db" / "guides.sqlite")
    cg.STORE = tmp / "guides"
    cg.SLOT_DIR = tmp / "gslots"
    (tmp / "db").mkdir(exist_ok=True)
    conn = init_db(str(tmp / "db" / "clinic.sqlite"))
    pids = {"paola_a": patient_id.seed_patient(conn, CF["paola_a"], "Paola Bianchi"),
            "paola_b": patient_id.seed_patient(conn, CF["paola_b"], "Paola Bianchi"),
            "mario": patient_id.seed_patient(conn, CF["mario"], "Mario Neri"),
            "vera": patient_id.seed_patient(conn, CF["vera"], "Vera Grigi")}
    gids = None
    if guides and TOOLS:
        g = cg.connect()
        gids, _devices = gf.load(g, gf.build(tmp / "library"))
        g.close()
    return conn, pids, gids


def refused(fn, code=None, perm=False):
    try:
        fn()
    except PermissionError:
        assert perm, "refused with PermissionError where a state error was expected"
        return "perm"
    except (ir.RequestError, LookupError) as e:
        assert not perm, f"expected a permission refusal, got {e}"
        if code:
            assert getattr(e, "code", "lookup") == code, f"refused for {getattr(e, 'code', 'lookup')}, not {code}"
        return getattr(e, "code", "lookup")
    raise AssertionError("HARD FAIL - the action was not refused")


def service(tmp):
    conn, pids, gids = setup(tmp)
    pa, pb, mario = pids["paola_a"], pids["paola_b"], pids["mario"]

    # C16 reception and admin can neither create nor change a request
    for who in (A, ADM):
        refused(lambda: ir.create_draft(conn, pa, "opg", "", "", *who, token="t-x"), perm=True)
    # C02 no request: reception gets the one sentence, nothing current
    assert ir.for_patient(conn, pa, *A) == [], "C02: nothing is current for a patient with no request"
    assert ir.NO_REQUEST == "No active imaging request is recorded for this patient; ask the dentist."

    # C03 a draft is never a current request
    rid, warn = ir.create_draft(conn, pa, "opg", "", "left side of the arch", *D, token="t-1", now=T0)
    assert warn == [] and ir.get(conn, rid)["state"] == "draft"
    assert ir.for_patient(conn, pa, *A) == [], "C03: HARD FAIL - reception sees a draft"
    h = ir.handoff(conn, pa, rid, *A)
    assert h["current"] is None and h["message"].startswith("This request is a draft"), "C03: a draft is not handed off"
    refused(lambda: ir.acknowledge(conn, rid, pa, 1, *A), "not_active")

    # C07 double submit: the same one-time token makes one draft
    again, _w = ir.create_draft(conn, pa, "opg", "", "left side of the arch", *D, token="t-1", now=T0)
    assert again == rid and conn.execute("SELECT COUNT(*) FROM imaging_requests").fetchone()[0] == 1, \
        "C07: HARD FAIL - a resubmitted form made a second request"
    # C17 a competing draft for the same patient and type is shown to the dentist, not merged
    rid2, warn = ir.create_draft(conn, pa, "opg", "", "", *D2, token="t-2", now=T0)
    assert rid2 != rid and warn and "already" in warn[0], "C17: a competing draft is warned about"

    refused(lambda: ir.activate(conn, rid2, pa, 5, *D), "stale")   # a page showing another version cannot activate
    # C10 / C01 the request is locked to its patient: another patient's id, even a same-name one, finds nothing
    for fn in (lambda: ir.activate(conn, rid, pb, 1, *D), lambda: ir.handoff(conn, pb, rid, *A),
               lambda: ir.cancel(conn, rid, mario, 1, "x", *D)):
        refused(fn, "lookup")

    # C06 two dentists activate the same draft at the same moment: one transition
    db = conn.execute("PRAGMA database_list").fetchone()[2]
    outcomes, barrier = [], threading.Barrier(2)

    def go(who):
        from storage import connect
        c = connect(db)
        barrier.wait()
        try:
            ir.activate(c, rid, pa, 1, *who, now=T0)
            outcomes.append("ok")
        except ir.RequestError as e:
            outcomes.append(e.code)
        finally:
            c.close()
    threads = [threading.Thread(target=go, args=(w,)) for w in (D, D2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(outcomes) == ["not_draft", "ok"], f"C06: HARD FAIL - {outcomes}"
    # C07 a second click on Activate changes nothing
    refused(lambda: ir.activate(conn, rid, pa, 1, *D), "not_draft")
    events = [e["action"] for e in ir.history(conn, rid, pa, *D)]
    assert events.count("activated") == 1, f"C07: HARD FAIL - {events}"

    # C04 the active request is what reception sees, exactly as recorded
    current = ir.for_patient(conn, pa, *A)
    assert [r["id"] for r in current] == [rid], "C04: reception sees the active request"
    h = ir.handoff(conn, pa, rid, *A)
    r = h["request"]
    winner = next(e["actor"] for e in ir.history(conn, rid, pa, *D) if e["action"] == "activated")
    assert h["current"] == rid and r["exam_label"] == "OPG (panoramic) - demo" and r["activated_by"] == winner \
        and winner in ("drossi", "dbianchi") and r["version"] == 1 and r["patient_id"] == pa, \
        "C04: the handoff shows the record's own facts (the dentist who won C06's race)"
    ir.acknowledge(conn, rid, pa, 1, *A)
    ir.acknowledge(conn, rid, pa, 1, *A)
    assert [e["action"] for e in ir.history(conn, rid, pa, *D)].count("acknowledged") == 1, "C07: one acknowledgement"
    ir.ask_dentist(conn, rid, pa, *A)
    assert "question" in [e["action"] for e in ir.history(conn, rid, pa, *D)], "reception can ask the dentist"

    # C05 a second type for the same patient is listed separately, never merged into "an OPG is ordered"
    bw, _w = ir.create_draft(conn, pa, "bitewing", "", "", *D, token="t-3", now=T0)
    ir.activate(conn, bw, pa, 1, *D, now=T0)
    listed = ir.for_patient(conn, pa, *A)
    assert sorted(r["exam"] for r in listed) == ["bitewing", "opg"] and len({r["id"] for r in listed}) == 2, \
        "C05: each active request is its own line"

    # C09 a revision is a new version; it replaces the old one only when the dentist activates it
    rev, _w = ir.revise(conn, rid, pa, "opg", "", "right side instead", *D, token="t-4", now=T0)
    assert ir.get(conn, rev)["version"] == 2 and ir.get(conn, rid)["state"] == "active", \
        "C09: the active version stays until the revision is activated"
    assert [r["id"] for r in ir.for_patient(conn, pa, *A) if r["exam"] == "opg"] == [rid]
    ir.activate(conn, rev, pa, 2, *D, now=T0)
    assert ir.get(conn, rid)["state"] == "superseded" and ir.get(conn, rev)["state"] == "active"
    old = ir.handoff(conn, pa, rid, *A)
    assert old["current"] is None and old["replaced_by"] == rev and "superseded" in old["message"], \
        "C09: an old version points to the current one and hands nothing off"
    refused(lambda: ir.acknowledge(conn, rid, pa, 1, *A), "not_active")
    refused(lambda: ir.acknowledge(conn, rev, pa, 1, *A), "stale")
    # C18 the superseded version cannot be activated or revised back to life
    refused(lambda: ir.activate(conn, rid, pa, 1, *D), "not_draft")
    refused(lambda: ir.revise(conn, rid, pa, "opg", "", "", *D, token="t-5"), "not_current")
    # competing revisions: only the first activated one wins; the other is stale
    r3, _w = ir.revise(conn, rev, pa, "opg", "", "a", *D, token="t-6")
    r4, _w = ir.revise(conn, rev, pa, "opg", "", "b", *D2, token="t-7")
    ir.activate(conn, r3, pa, 3, *D)
    refused(lambda: ir.activate(conn, r4, pa, 4, *D2), "stale")
    rev = r3

    # C08 cancellation is immediate and cannot be undone by a stale page
    ir.cancel(conn, rev, pa, 3, "dentist changed the plan", *D, now=T0)
    assert [r["exam"] for r in ir.for_patient(conn, pa, *A)] == ["bitewing"], "C08: a cancelled request is gone"
    h = ir.handoff(conn, pa, rev, *A)
    assert h["current"] is None and "cancelled" in h["message"], "C08: the handoff says it was cancelled"
    refused(lambda: ir.acknowledge(conn, rev, pa, 3, *A), "not_active")
    refused(lambda: ir.activate(conn, rev, pa, 3, *D), "not_draft")
    refused(lambda: ir.cancel(conn, rev, pa, 3, "again", *D), "not_open")
    for who in (A, ADM):
        refused(lambda: ir.cancel(conn, bw, pa, 1, "x", *who), perm=True)
        refused(lambda: ir.activate(conn, rid2, pa, 1, *who), perm=True)
    assert "note" not in " ".join(str(e) for e in ir.history(conn, rev, pa, *D)), "history carries no note text"
    audit = conn.execute("SELECT target, reason FROM audit_log WHERE action LIKE 'imaging_%'").fetchall()
    for target, reason in audit:
        assert "right side" not in (reason or "") and CF["paola_a"] not in (target or ""), "audit carries note text or a code"
    return conn, pids, gids, bw


def guidance(conn, pids, gids, bw):
    # C04 / C12 / C13 / C05 / C20: generic steps come only from an approved, current, visible P24 page
    if not TOOLS:
        print("SKIPPED guidance cases C12 C13 C20 (sips, tesseract or sandbox-exec missing)")
        return
    pa = pids["paola_a"]
    opg, _w = ir.create_draft(conn, pa, "opg", "", "", *D, token="t-g1")
    ir.activate(conn, opg, pa, 1, *D)
    g = cg.connect()
    # P24 follow-up 3: RP-01's steps end with "confirm by phone 2 days before", which the front desk handbook gives as
    # 1 day - no step another approved document contradicts is shown, so with both approved there are no steps
    got = ir.guidance(g, "opg", "assistant")
    assert got["citation"] is None and got["why"] == cg.MESSAGES["conflict"], \
        f"C04: HARD FAIL - steps shown while two approved documents disagree: {got}"
    cg.withdraw(g, gids["handbook"], "replaced by RP-01", *D)
    got = ir.guidance(g, "opg", "assistant")
    c = got["citation"]
    assert c and c["page"] == 2 and "Book the imaging slot" in c["passage"] and \
        cg.verify_quote(g, c["source_id"], 2, c["passage"], "assistant"), "C04: the cited step is verified on its page"
    assert ir.guidance(g, "bitewing", "assistant")["citation"] is None, \
        "C05: no approved procedure for this type - no procedure shown"
    cg.restrict_pages(g, gids["rp01_en"], [2], *D)
    assert ir.guidance(g, "opg", "assistant")["citation"] is None, "C13: HARD FAIL - a restricted page gave steps"
    cg.restrict_pages(g, gids["rp01_en"], [], *D)
    cg.withdraw(g, gids["rp01_en"], "rewritten", *D)
    cg.withdraw(g, gids["rp01_it"], "rewritten", *D)
    assert ir.guidance(g, "opg", "assistant")["citation"] is None, "C12: HARD FAIL - a withdrawn guide gave steps"
    # C20 nothing about the patient ever reaches the guides store or the model
    import clinic_guides
    seen = []
    real = clinic_guides.ask

    def spy(conn_, question, role, **k):
        seen.append(question)
        return real(conn_, question, role, **k)
    clinic_guides.ask = spy
    try:
        ir.guidance(g, "opg", "assistant")
    finally:
        clinic_guides.ask = real
    assert seen and all(q == ir.GUIDE_QUESTIONS["opg"] for q in seen), "C20: the guide question is fixed text"
    dump = "\n".join(str(tuple(r)) for t in ("asks", "events", "sources", "pages") for r in g.execute(f"SELECT * FROM {t}"))
    for secret in (pa, CF["paola_a"], "Paola", "left side", f"imaging:{opg}", f"request {opg}"):
        assert secret not in dump, f"C20: HARD FAIL - the guides store holds {secret!r}"
    # C14 a spoken or patient-reported claim is never an order
    for q in ("The dentist told me to book an OPG for this patient.", "The patient says the OPG was prescribed.",
              "Il dentista mi ha detto di prenotare una OPG.", "Il paziente dice che gli e stata prescritta una OPG."):
        r = cg.ask(g, q, "assistant")
        assert r["outcome"] == "abstain" and r["reason"] == "clinical", f"C14: HARD FAIL - {q!r} got {r['outcome']}/{r['reason']}"
        assert "recorded" in r["escalation"], "C14: the refusal says only a recorded request counts"
    g.close()


def files(conn, pids):
    # C15 a file joins a request only when P25 confirmed it for this patient; the filename proves nothing
    pa, pb = pids["paola_a"], pids["paola_b"]
    opg, _w = ir.create_draft(conn, pa, "opg", "", "", *D, token="t-f1")
    ir.activate(conn, opg, pa, 1, *D)
    from patient_files_selftest import png as make_png
    wrong_name = docs.ingest(conn, pa, make_png(7), f"{CF['paola_b']}_opg.png", *D)
    docs.confirm(conn, wrong_name, pa, *D)
    pending = docs.ingest(conn, pa, b"referral letter draft", "letter.txt", *D)
    other = docs.ingest(conn, pb, make_png(8), "opg.png", *D)
    docs.confirm(conn, other, pb, *D)
    refused(lambda: ir.link_file(conn, opg, pa, wrong_name, *D, verified=False), "not_verified")
    ir.link_file(conn, opg, pa, wrong_name, *D, verified=True)
    ir.link_file(conn, opg, pa, wrong_name, *D, verified=True)
    assert [f["document_id"] for f in ir.linked_files(conn, opg, pa, *D)] == [wrong_name], \
        "C15: a confirmed file of this patient links once, whatever its name says"
    assert not pf.published(conn, wrong_name, pa), "C15: HARD FAIL - linking published the file"
    assert docs.row(conn, wrong_name)["status"] == "confirmed", "C15: linking changed nothing about the file"
    refused(lambda: ir.link_file(conn, opg, pa, pending, *D, verified=True), "not_confirmed")
    refused(lambda: ir.link_file(conn, opg, pa, other, *D, verified=True), "lookup")
    refused(lambda: ir.link_file(conn, opg, pb, other, *D, verified=True), "lookup")
    refused(lambda: ir.link_file(conn, opg, pa, wrong_name, *A, verified=True), perm=True)
    return opg, wrong_name


def lifecycle(tmp, conn, pids, opg, doc):
    # C19 backup/restore keeps states, versions, history and links; merge and erasure follow the patient
    import backup
    import erasure
    import patient_identity
    root = Path(tmp)
    key = root / "k"
    backup.init_key(key)
    conn.commit()
    archive = backup.create(data_root=root, dest=root / "backups", key_path=key)["archive"]
    backup.restore(archive, root / "restored", key, apply=True)
    rc = sqlite3.connect(root / "restored" / "db" / "clinic.sqlite")
    for table in ("imaging_requests", "imaging_request_events", "imaging_request_files"):
        restored = rc.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        live = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        assert live > 0 and restored == live, f"C19: {table} not restored ({restored} of {live})"
    assert rc.execute("SELECT state FROM imaging_requests WHERE id = ?", (opg,)).fetchone()[0] == "active"
    rc.close()
    pa, pb, vera = pids["paola_a"], pids["paola_b"], pids["vera"]
    vb, _w = ir.create_draft(conn, pb, "periapical", "", "", *D, token="t-m1")
    ir.activate(conn, vb, pb, 1, *D)
    ok, msg = patient_identity.merge(conn, CF["paola_b"], CF["paola_a"], "anadmin", "admin", sorted_root=root / "sorted")
    assert ok, msg
    assert ir.get(conn, vb)["patient_id"] == pa, "C19: a merge brings the folded record's requests"
    assert {r["exam"] for r in ir.for_patient(conn, pa, *A)} >= {"periapical", "opg"}, "C19: listed separately"
    assert ir.linked_files(conn, opg, pa, *D)[0]["document_id"] == doc, "C19: a link stays with its request"
    vr, _w = ir.create_draft(conn, vera, "cbct", "", "", *D, token="t-e1")
    ir.activate(conn, vr, vera, 1, *D)
    erasure.erase(conn, vera, "drossi", "dentist", req_id=None, sorted_root=root / "sorted", drop_dir=root / "drop",
                  undo_log=root / "u.jsonl", exports_dir=root / "exports", tombstones=root / "t", write_tombstone=False)
    for table in ("imaging_requests", "imaging_request_events", "imaging_request_files"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table} WHERE patient_id = ?", (vera,)).fetchone()[0] == 0, \
            f"C19: erasure left rows in {table}"
    # the data export is unchanged by P26 (P15-D2 / P26-D2 open)
    import data_rights
    import zipfile
    import json
    name = data_rights.build_export(conn, pa, sorted_root=root / "sorted", exports_dir=root / "exports")
    data = json.loads(zipfile.ZipFile(root / "exports" / name).read("data.json"))
    assert "imaging" not in json.dumps(data).lower(), "C19: the export was broadened without a decision"


def routes(tmp):
    # C10 C11 C16 through the staff routes; the patient portal and the site have no route
    import app.db as app_db
    import web_session
    from app import create_app
    from werkzeug.security import generate_password_hash
    tmp = Path(tmp)
    conn, pids, _g = setup(tmp, guides=False)
    db_path = str(tmp / "db" / "clinic.sqlite")
    app_db.DB_PATH = db_path
    app_db.CHROMA_PATH = str(tmp / "chroma")
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
        return cl, token
    (den, _t), (ast, _t2), (adm, _t3) = client(*D), client(*A), client(*ADM)

    def csrf(html):
        return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
    cfa, cfb = CF["paola_a"], CF["paola_b"]
    rec = ast.get(f"/patients/{cfa}").text
    assert ir.NO_REQUEST in rec.replace("&#39;", "'"), "C02: reception's empty state on the record"
    assert "New demo imaging request" not in rec and f"/patients/{cfa}/imaging\"" not in rec, \
        "C16: reception is never offered the create form"
    page = den.get(f"/patients/{cfa}").text
    assert "New demo imaging request" in page, "the dentist creates from the record"
    token = re.search(r'name="submit_token" value="([^"]+)"', page).group(1)
    form = {"csrf_token": csrf(page), "exam": "opg", "label": "", "note": "", "submit_token": token}
    den.post(f"/patients/{cfa}/imaging", data=form)
    den.post(f"/patients/{cfa}/imaging", data=form)
    rows = conn.execute("SELECT * FROM imaging_requests").fetchall()
    assert len(rows) == 1 and rows[0]["state"] == "draft", "C07: a double-clicked form makes one draft"
    rid = rows[0]["id"]
    # reception and admin cannot activate through the route
    ast.post(f"/patients/{cfa}/imaging/{rid}/activate", data={"csrf_token": csrf(rec), "version": "1"})
    assert ir.get(conn, rid)["state"] == "draft", "C16: HARD FAIL - reception activated a request"
    assert adm.get(f"/patients/{cfa}/imaging/{rid}").status_code in (302, 403), "C16: admin opened a request"
    page = den.get(f"/patients/{cfa}/imaging/{rid}").text
    assert "Paola Bianchi" in page and cfa in page and "Activate" in page, "the dentist sees whose request it is"
    den.post(f"/patients/{cfa}/imaging/{rid}/activate", data={"csrf_token": csrf(page), "version": "1"})
    assert ir.get(conn, rid)["state"] == "active", "the dentist activates through the route"
    h = ast.get(f"/patients/{cfa}/imaging/{rid}")
    assert h.status_code == 200 and "OPG (panoramic) - demo" in h.text and "drossi" in h.text and \
        "no-store" in h.headers.get("Cache-Control", ""), "C04: reception's handoff page"
    assert 'action="/patients/' + cfa + f'/imaging/{rid}/activate"' not in h.text and "Cancel request" not in h.text, \
        "reception has no edit controls"
    # C10 another patient in the URL - same answer as a request that does not exist
    for who, url, gone in ((ast, f"/patients/{cfb}/imaging/{rid}", f"/patients/{cfa}/imaging/99999"),
                           (den, f"/patients/{cfb}/imaging/{rid}", f"/patients/{cfa}/imaging/99999"),
                           (den, f"/patients/{cfb}/imaging/{rid}/files", f"/patients/{cfa}/imaging/99999/files")):
        other, missing = who.get(url), who.get(gone)
        assert other.status_code == 404 and other.data == missing.data, f"C10: HARD FAIL - {url} answered differently"
    assert ast.get(f"/patients/{cfa}/imaging/{rid}/files").status_code == 302, "the file-link page is the dentist's"
    # a forged version refuses; the dentist's stale page cannot act
    den.post(f"/patients/{cfa}/imaging/{rid}/cancel", data={"csrf_token": csrf(page), "version": "7", "reason": "x"})
    assert ir.get(conn, rid)["state"] == "active", "a forged version cancels nothing"
    # CSRF missing: refused
    r = den.post(f"/patients/{cfa}/imaging/{rid}/cancel", data={"version": "1", "reason": "x"})
    assert r.status_code == 400 and ir.get(conn, rid)["state"] == "active", "a post without CSRF changes nothing"
    # C11 an expired session reads nothing and changes nothing
    c = sqlite3.connect(db_path)
    c.execute("DELETE FROM sessions")
    c.commit()
    c.close()
    r = ast.get(f"/patients/{cfa}/imaging/{rid}")
    assert r.status_code == 302 and "/login" in r.headers["Location"], "C11: an expired session goes to sign-in"
    from patient_app import create_patient_app
    import site_app
    for rules in ([r.rule for r in create_patient_app(env_path=tmp / ".env.patient").url_map.iter_rules()],
                  [r.rule for r in site_app.create_site_app().url_map.iter_rules()]):
        assert not any("imaging" in r for r in rules), "no patient-portal or site route"
    conn.close()


def selftest():
    with tempfile.TemporaryDirectory() as tmp:
        conn, pids, gids, bw = service(tmp)
        guidance(conn, pids, gids, bw)
        opg, doc = files(conn, pids)
        lifecycle(tmp, conn, pids, opg, doc)
        conn.close()
    with tempfile.TemporaryDirectory() as tmp:
        routes(tmp)
    print("imaging_requests_selftest: ok")


if __name__ == "__main__":
    selftest()
    sys.exit(0)
