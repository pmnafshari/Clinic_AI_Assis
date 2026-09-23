"""Clinical dictation and read-aloud (P14, safe half), one claim per check.

No speech model exists on this machine, so the engines are the `sandbox`
adapters: the sandbox "hears" an upload's bytes as UTF-8 text and "speaks" by
returning a fixed marker. Every connection is recorded at the socket layer.
"""
import json
import re
import socket
import sqlite3
import sys
import tempfile
from pathlib import Path

import clinic_time
import dictation as dc
import patient_id
from auth import authorize

ROOT = Path(__file__).resolve().parent
SANDBOX = {dc.ENV_STT: "sandbox", dc.ENV_TTS: "sandbox"}


class NoNetwork:
    def __enter__(self):
        self.hosts = []
        self.saved = (socket.create_connection, socket.socket.connect)
        rec = self

        def create_connection(address, *a, **k):
            rec.hosts.append(str(address[0]))
            raise ConnectionRefusedError("recorded")

        def connect(sock, address):
            rec.hosts.append(str(address))
            raise ConnectionRefusedError("recorded")

        socket.create_connection, socket.socket.connect = create_connection, connect
        return self

    def __exit__(self, *exc):
        socket.create_connection, socket.socket.connect = self.saved
        return False


def domain(tmp):
    # 1. nothing is configured by default, and no cloud engine can be named
    assert dc.stt_adapter({}).name == "disabled" and dc.tts_adapter({}).name == "disabled"
    ok, why = dc.stt_status({})
    assert not ok and "no approved local speech model" in why, why
    for name in ("deepgram", "elevenlabs", "openai", "google", "azure", "whisper-api", "live"):
        for env_key, fn in ((dc.ENV_STT, dc.stt_adapter), (dc.ENV_TTS, dc.tts_adapter)):
            assert fn({env_key: name}).name == "disabled", f"1: {name} resolved to an engine"
    # the sandbox never runs in production, whatever the variable says
    assert dc.stt_adapter({dc.ENV_STT: "sandbox", "CLINIC_ENV": "production"}).name == "disabled"

    # 2. the dictation module cannot reach the network or the cloud voice demo:
    # no such import, no URL in any string. (its docstring may say why not.)
    import ast
    tree = ast.parse((ROOT / "dictation.py").read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    for forbidden in ("voice", "voice_config", "urllib", "requests", "http", "socket",
                      "local_model", "extract_note"):
        assert forbidden not in imported, f"2: dictation.py imports {forbidden}"
    doc = tree.body[0].value if isinstance(tree.body[0], ast.Expr) else None
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node is not doc:
            assert "http" not in node.value.lower() and "deepgram" not in node.value.lower(), \
                f"2: a URL or cloud engine in {node.value!r}"

    # 3. transcription: bounded, unclear audio is an alert, disabled says why
    with NoNetwork() as net:
        assert dc.transcribe(b"otturazione dente 36", "it", env=SANDBOX) == "otturazione dente 36"
        for silent in (b"", b"   \n"):
            try:
                dc.transcribe(silent, "it", env=SANDBOX)
                raise AssertionError("3: silence became a transcript")
            except dc.Unclear:
                pass
        try:
            dc.transcribe(b"x" * (dc.MAX_AUDIO_BYTES + 1), "it", env=SANDBOX)
            raise AssertionError("3: an oversized recording was accepted")
        except dc.Unavailable as e:
            assert "too long" in str(e)
        try:
            dc.transcribe(b"otturazione", "it", env={})
            raise AssertionError("3: transcribed with nothing configured")
        except dc.Unavailable as e:
            assert "type the note" in str(e), e
    assert not net.hosts, f"3: DICTATION OPENED A CONNECTION: {net.hosts}"

    # 4. what may be read aloud: the current approved, unchanged summary of
    # this patient only. drafts, stale, other patients and wrong roles: nothing
    import visit_summary as vs
    from storage import init_db
    conn = init_db(str(Path(tmp) / "d.sqlite"))
    import note_review
    t0 = clinic_time.read_instant("2026-09-23T08:00:00+00:00")
    anna = patient_id.seed_patient(conn, "ZZDA000000000001", "Anna Dettata")
    bruno = patient_id.seed_patient(conn, "ZZDB000000000002", "Bruno Dettato")
    vid = conn.execute("INSERT INTO visits (patient_id, visit_date, procedures, clinical_notes,"
                       " source_path) VALUES (?, '2026-02-02', '[\"prophy\"]', 'pulizia', 'd1.json')",
                       (anna,)).lastrowid
    conn.commit()
    note_review.mark_reviewed(conn, vid, "typed", "drossi")
    sid = vs.generate(conn, anna, "drossi", "dentist", now=t0)
    assert dc.readable_summary(conn, anna, "drossi", "dentist") is None, "4: a DRAFT is readable"
    vs.approve(conn, sid, anna, "drossi", "dentist", now=t0)
    text = dc.readable_summary(conn, anna, "drossi", "dentist")
    assert text and "pulizia" in text and "[#" not in text, f"4: {text!r}"
    assert dc.readable_summary(conn, bruno, "drossi", "dentist") is None, "4: another patient's"
    for role in ("assistant", "admin"):
        try:
            dc.readable_summary(conn, anna, "x", role)
            raise AssertionError(f"4: {role} may have a summary read aloud")
        except PermissionError:
            pass
    conn.execute("UPDATE visits SET clinical_notes = 'pulizia e lucidatura' WHERE id = ?", (vid,))
    conn.commit()
    assert dc.readable_summary(conn, anna, "drossi", "dentist") is None, "4: a STALE summary"

    # 5. speaking: never without the private-space confirmation, disabled says why
    try:
        dc.speak("testo", private_confirmed=False, env=SANDBOX)
        raise AssertionError("5: spoke without the private-space confirmation")
    except dc.NotPrivate:
        pass
    try:
        dc.speak("testo", private_confirmed=True, env={})
        raise AssertionError("5: spoke with no engine")
    except dc.Unavailable as e:
        assert "no approved local voice" in str(e), e
    with NoNetwork() as net:
        audio = dc.speak("testo", private_confirmed=True, env=SANDBOX)
    assert audio.startswith(b"SANDBOX-AUDIO") and not net.hosts
    conn.close()

    # 6. dentist, reception and admin: who may do what
    assert authorize("dentist", "append_note") and authorize("assistant", "append_note")
    assert not authorize("admin", "append_note"), "6: admin may dictate a clinical note"


def routes(tmp):
    import os
    from werkzeug.security import generate_password_hash

    import app.db as app_db
    import web_session
    from app import create_app

    db_path = str(Path(tmp) / "r.sqlite")
    app_db.DB_PATH = db_path
    app_db.CHROMA_PATH = str(Path(tmp) / "chroma")
    app = create_app()
    app.config["TESTING"] = True
    from storage import init_db
    conn = init_db(db_path)
    cf = "ZZDC000000000003"
    pid = patient_id.seed_patient(conn, cf, "Carla Dettata")

    def client(username, role):
        conn.execute("INSERT OR IGNORE INTO users (username, password_hash, role, active)"
                     " VALUES (?, ?, ?, 1)", (username, generate_password_hash("x"), role))
        conn.commit()
        c = app.test_client()
        c.set_cookie(web_session.COOKIE_NAME, web_session.create_session(conn, username, role))
        return c

    def csrf(html):
        return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)

    dentist, admin = client("dc_dentist", "dentist"), client("dc_admin", "admin")
    visits_before = conn.execute("SELECT COUNT(*) FROM visits").fetchone()[0]

    # 7. with nothing configured the note form says dictation is unavailable
    # and why, and typing still works; no recording control is offered
    page = dentist.get(f"/notes/new?cf={cf}").text
    assert "Dictation is not available" in page and 'name="audio"' not in page, "7"

    saved = {k: os.environ.get(k) for k in (dc.ENV_STT, dc.ENV_TTS)}
    os.environ.update(SANDBOX)
    try:
        page = dentist.get(f"/notes/new?cf={cf}").text
        assert 'name="audio"' in page, "7: the sandbox recording control is missing"

        # 8. a dictated note arrives as TEXT in the form for the locked patient,
        # unsaved. nothing reaches visits until the existing confirm step
        with NoNetwork() as net:
            out = dentist.post("/notes/dictate", data={
                "csrf_token": csrf(page), "cf": cf,
                "audio": (__import__("io").BytesIO(b"otturazione dente 36 fu 2wk"), "a.webm")},
                content_type="multipart/form-data").text
        assert not [h for h in net.hosts if h not in ("localhost", "127.0.0.1", "::1")], net.hosts
        assert "otturazione dente 36 fu 2wk" in out, "8: the transcript is not in the form"
        assert "Dictated - check it" in out, "8: the draft is not labelled"
        assert cf in out, "8: the patient is not locked"
        assert conn.execute("SELECT COUNT(*) FROM visits").fetchone()[0] == visits_before, \
            "8: A DICTATION WAS SAVED WITHOUT CONFIRMATION"

        # 9. unclear audio is an alert, not an empty note
        out = dentist.post("/notes/dictate", data={
            "csrf_token": csrf(page), "cf": cf,
            "audio": (__import__("io").BytesIO(b"   "), "a.webm")},
            content_type="multipart/form-data").text
        assert "could not make out" in out, "9: unclear audio was not flagged"

        # 10. admin cannot dictate at all; a dictated command only fills the
        # command box and executes nothing
        a_page = admin.get("/", follow_redirects=True).text
        r = admin.post("/notes/dictate", data={"csrf_token": csrf(a_page), "cf": cf,
                                               "audio": (__import__("io").BytesIO(b"x"), "a.webm")},
                       content_type="multipart/form-data")
        assert r.status_code == 302, "10: admin dictated"
        cpage = dentist.get("/agent/command").text
        out = dentist.post("/agent/dictate", data={
            "csrf_token": csrf(cpage),
            "audio": (__import__("io").BytesIO(b"cambia il telefono di Carla"), "a.webm")},
            content_type="multipart/form-data").text
        assert "cambia il telefono di Carla" in out and "Confirm" not in out, \
            "10: a dictated command did more than fill the box"
        assert conn.execute("SELECT COUNT(*) FROM pending_actions").fetchone()[0] == 0, \
            "10: a dictated command created a pending action"

        # 10b. P14.T2 end to end: dictate -> extract -> confirm, then the same
        # confirm again. one note, filed under the locked patient.
        from app import notes_routes
        import io as _io

        class _Reply:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self):
                return json.dumps({"response": json.dumps({
                    "patient_name": "Carla Dettata", "codice_fiscale": "", "phone": None,
                    "visit_date": "2026-09-01", "procedures": ["filling 36"], "invoices": [],
                    "clinical_notes": "otturazione dente 36", "next_appointment": "2wk"})}).encode()

        saved_open, saved_root = notes_routes._urlopen, notes_routes.SORTED_ROOT
        notes_routes._urlopen = lambda req, timeout=None: _Reply()
        notes_routes.SORTED_ROOT = Path(tmp) / "sorted"
        try:
            page = dentist.get(f"/notes/new?cf={cf}").text
            heard = dentist.post("/notes/dictate", data={
                "csrf_token": csrf(page), "cf": cf,
                "audio": (_io.BytesIO(b"otturazione dente 36 fu 2wk"), "a.webm")},
                content_type="multipart/form-data").text
            preview = dentist.post("/notes/new", data={
                "csrf_token": csrf(heard), "cf": cf, "raw_note": "otturazione dente 36 fu 2wk"}).text
            token = re.search(r'name="confirm_token" value="([^"]+)"', preview).group(1)
            form = {"csrf_token": csrf(preview), "cf": cf, "confirm_token": token,
                    "patient_name": "x", "codice_fiscale": "ZZDZ000000000009",
                    "visit_date": "2026-09-01", "clinical_notes": "otturazione dente 36",
                    "procedures": "filling 36", "next_appointment": "2wk"}
            first = dentist.post("/notes/new", data=form)
            again = dentist.post("/notes/new", data=form)
            assert first.status_code == 302 and again.status_code == 302, (first, again)
            rows = conn.execute("SELECT patient_id FROM visits WHERE clinical_notes ="
                                " 'otturazione dente 36'").fetchall()
            assert len(rows) == 1, f"10b: A RETRIED CONFIRM SAVED {len(rows)} NOTES"
            assert rows[0][0] == pid, "10b: the dictated note went to another patient"
        finally:
            notes_routes._urlopen, notes_routes.SORTED_ROOT = saved_open, saved_root

        # 10c. read aloud: only with the private confirmation, only an approved
        # current summary, and the page offers a player - it never autoplays
        import note_review
        import visit_summary as vs
        for (vid,) in conn.execute("SELECT id FROM visits WHERE patient_id = ?", (pid,)).fetchall():
            note_review.mark_reviewed(conn, vid, "typed", "dc_dentist")
        sid = vs.generate(conn, pid, "dc_dentist", "dentist")
        spage = dentist.get(f"/patients/{cf}/summary").text
        assert "Prepare to read aloud" not in spage, "10c: a draft is offered for reading aloud"
        vs.approve(conn, sid, pid, "dc_dentist", "dentist")
        spage = dentist.get(f"/patients/{cf}/summary").text
        assert "Prepare to read aloud" in spage and "I am somewhere private" in spage
        out = dentist.post(f"/patients/{cf}/summary/read", data={"csrf_token": csrf(spage)},
                           follow_redirects=True).text
        assert "somewhere private" in out and "<audio" not in out, "10c: read without confirmation"
        out = dentist.post(f"/patients/{cf}/summary/read",
                           data={"csrf_token": csrf(spage), "private": "yes"}).text
        assert "<audio controls" in out and "autoplay" not in out, "10c: player missing or autoplays"

        # 11. no audio is kept: no file in the working tree or temp dir holds it,
        # and no table does
        marker = b"ZZAUDIOMARKER-91c"
        dentist.post("/notes/dictate", data={"csrf_token": csrf(page), "cf": cf,
                                             "audio": (__import__("io").BytesIO(marker), "a.webm")},
                     content_type="multipart/form-data")
        for table in [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]:
            blob = json.dumps([tuple(map(str, row)) for row in conn.execute(f'SELECT * FROM "{table}"')])
            assert marker.decode() not in blob, f"11: dictated audio/text stored in {table}"
        for folder in (Path(tmp), ROOT / "drop", ROOT / "staging"):
            if folder.exists():
                for f in folder.rglob("*"):
                    if f.is_file() and f.stat().st_size < 5_000_000:
                        assert marker not in f.read_bytes(), f"11: audio kept in {f}"
        # every dictation is audited, without its content
        rows = conn.execute("SELECT target, reason FROM audit_log WHERE action = 'dictate'").fetchall()
        assert rows and all(marker.decode() not in json.dumps(tuple(r)) for r in rows), "11: audit"
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    conn.close()


def exposure():
    # 12. the patient portal, public site, phone and reminders have no dictation
    for path in [*ROOT.glob("patient_app/*.py"), *ROOT.glob("site_app/*.py"),
                 ROOT / "calls.py", ROOT / "reminders.py"]:
        assert "dictation" not in path.read_text(), f"12: {path.name}"


def selftest():
    with tempfile.TemporaryDirectory() as tmp:
        domain(tmp)
    with tempfile.TemporaryDirectory() as tmp:
        routes(tmp)
    exposure()
    print("selftest ok")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
    else:
        print("usage: python dictation_selftest.py --selftest")
        sys.exit(1)
