"""J03, first slice (owner-authorised 2026-10-11): a note added to a patient's record by voice, through the clinic's
own checks. The device credential alone is never enough: a staff member's live session must be delegated to the device,
and the account's current role must allow adding notes. The spoken patient name only proposes patients - the person at
the screen picks the patient and saves; a draft is used once, by its own device and delegation, within its time.

    .venv/bin/python jarvis_notes_selftest.py

1. without a delegated session: refused, audited, no patient disclosed, nothing drafted or saved
2. a dentist's delegation: one patient matches - the draft names it; saving files the spoken words on that patient
   (through the clinic's own save), audited as a Jarvis voice note
3. same-name patients: every match is offered on screen, none chosen; too many matches or none: refused with nothing
   listed (none) - say more of the name
4. a draft is used once, by its own device, before it expires; a patient outside the draft's matches is refused
5. the delegation ends between draft and save: nothing is saved
6. over HTTP: device credential only, the routes are the clinic's own, every call audited; the note lands in the record
"""
import json
import sqlite3
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

import app.db as app_db
import clinic_time
import jarvis_link as jl
import web_session
from patient_files_selftest import csrf, staff_client

T0 = clinic_time.now_utc()
D, A, ADM = ("drossi", "dentist"), ("aassist", "assistant"), ("anadmin", "admin")
PATIENTS = [("SNTGLI51R49D612Y", "Giulia Santoro"), ("RSSP850010150900", "Paola Rossi"),
            ("RSPS850010150900", "Paola Rossi"), ("GLLMRC67A11F839U", "Marco Gallo")]
WORDS = "patient reports sensitivity on the lower left molar, review in two weeks"
PASSWORD = "jarvis-test-pass-1"


def refused(fn, code):
    try:
        fn()
    except jl.LinkError as e:
        assert e.code == code, f"refused with {e.code}, wanted {code}"
        return
    raise AssertionError(f"not refused (wanted {code})")


def setup(tmp):
    db_path = str(tmp / "clinic.sqlite")
    app_db.DB_PATH = db_path
    app_db.CHROMA_PATH = str(tmp / "chroma")
    from app import create_app
    import app.jarvis_routes as jr
    jr.SORTED_ROOT = tmp / "sorted"
    app = create_app()
    app.config["TESTING"] = True
    from patient_id import seed_patient
    from storage import connect
    conn = connect(db_path)
    conn.row_factory = sqlite3.Row
    for cf, name in PATIENTS:
        seed_patient(conn, cf, name)
    admin = staff_client(app, db_path, *ADM)
    for who in (D, A):
        staff_client(app, db_path, *who)                 # creates the account
    from werkzeug.security import generate_password_hash
    conn.execute("UPDATE users SET password_hash = ?, must_change_password = 0", (generate_password_hash(PASSWORD),))
    conn.commit()
    page = admin.get("/jarvis/devices").text
    admin.post("/jarvis/devices", data={"csrf_token": csrf(page), "name": "Surgery Mac"})
    token = jl.LAST_SHOWN_TOKEN
    admin.post("/jarvis/devices", data={"csrf_token": csrf(admin.get("/jarvis/devices").text), "name": "Other Mac"})
    other = jl.LAST_SHOWN_TOKEN
    return app, conn, db_path, token, other


def delegate(app, db_path, conn, token, who):
    client = staff_client(app, db_path, *who)
    page = client.get("/jarvis/link").text
    device = jl.authenticate(conn, token)["id"]
    client.post(f"/jarvis/link/{device}", data={"csrf_token": csrf(page)})
    return client


def visits(conn, cf):
    return conn.execute("SELECT v.clinical_notes FROM visits v JOIN patients p ON p.patient_id = v.patient_id"
                        " WHERE p.codice_fiscale = ?", (cf,)).fetchall()


def signin(app, conn, token, other):
    # 7. Owner, 2026-10-11: Jarvis is signed in with a doctor's own username and password - the clinic's own check,
    #    lockout and audit; never reception or admin; the sign-in lasts until the next 00:00 clinic time, with no
    #    staff browser session; signing out, or the account being deactivated or no longer a doctor, ends it
    device = jl.authenticate(conn, token)
    jl.logout(conn, device, "test")
    refused(lambda: jl.login(conn, device, "drossi", "wrong-password"), "credentials")
    refused(lambda: jl.login(conn, device, "aassist", PASSWORD), "not_doctor")
    refused(lambda: jl.login(conn, device, "anadmin", PASSWORD), "not_doctor")
    assert jl.current_delegation(conn, device["id"]) is None, "7: HARD FAIL - a refused sign-in delegated"
    now = clinic_time.read_instant("2026-10-11T20:30:00+00:00")
    who = jl.login(conn, device, "drossi", PASSWORD, now=now)
    local = clinic_time.to_local(clinic_time.read_instant(who["expires_at"]))
    assert local.hour == 0 and local.minute == 0 and \
        local.date() == clinic_time.to_local(now).date() + timedelta(days=1), \
        f"7: the sign-in ends at the next 00:00 clinic time ({local})"
    later = now + timedelta(hours=3)
    assert jl.current_delegation(conn, device["id"], later) is None or \
        clinic_time.read_instant(who["expires_at"]) > later, "7: valid until midnight"
    mid = clinic_time.read_instant(who["expires_at"])
    before = jl.current_delegation(conn, device["id"], mid - timedelta(seconds=1))
    assert before and before["username"] == "drossi" and before["role"] == "dentist", \
        "7: signed in without any browser session, until midnight"
    assert jl.current_delegation(conn, device["id"], mid) is None, "7: HARD FAIL - the sign-in outlived 00:00"
    assert jl.current_delegation(conn, jl.authenticate(conn, other)["id"], now) is None, "7: one device only"
    jl.login(conn, device, "drossi", PASSWORD)
    conn.execute("UPDATE users SET role = 'assistant' WHERE username = 'drossi'")
    conn.commit()
    assert jl.current_delegation(conn, device["id"]) is None, "7: HARD FAIL - kept after the account stopped being a doctor"
    conn.execute("UPDATE users SET role = 'dentist', active = 0 WHERE username = 'drossi'")
    conn.commit()
    assert jl.current_delegation(conn, device["id"]) is None, "7: HARD FAIL - kept for a deactivated account"
    conn.execute("UPDATE users SET active = 1 WHERE username = 'drossi'")
    conn.commit()
    jl.login(conn, device, "drossi", PASSWORD)
    jl.logout(conn, device, "test")
    assert jl.current_delegation(conn, device["id"]) is None, "7: signing out ends it at once"
    for _ in range(5):
        refused(lambda: jl.login(conn, device, "drossi", "wrong-password"), "credentials")
    refused(lambda: jl.login(conn, device, "drossi", PASSWORD), "credentials")     # locked, like the web sign-in
    conn.execute("UPDATE users SET failed_attempts = 0, locked_until = NULL WHERE username = 'drossi'")
    conn.commit()
    rows = conn.execute("SELECT action, allowed FROM audit_log WHERE action IN ('jarvis_signin', 'jarvis_signout')"
                        ).fetchall()
    assert any(r["action"] == "jarvis_signin" and r["allowed"] == 1 for r in rows) and \
        any(r["action"] == "jarvis_signin" and r["allowed"] == 0 for r in rows), "7: sign-ins audited, refusals too"
    raw = "\n".join(str(tuple(x)) for x in conn.execute("SELECT * FROM audit_log"))
    assert PASSWORD not in raw and "wrong-password" not in raw, "7: HARD FAIL - a password in the audit"
    anon = app.test_client()
    h = {"Authorization": f"Bearer {token}"}
    assert anon.post("/api/jarvis/login", json={"username": "drossi", "password": "x"}).status_code == 401
    r = anon.post("/api/jarvis/login", json={"username": "aassist", "password": PASSWORD}, headers=h)
    assert r.status_code == 403 and r.get_json()["error"] == "not_doctor", "7: reception refused over HTTP"
    assert anon.post("/api/jarvis/login", json={"username": "drossi", "password": PASSWORD}, headers=h).status_code == 200
    assert anon.post("/api/jarvis/logout", headers=h).status_code == 204
    assert jl.current_delegation(conn, device["id"]) is None, "7: signed out over HTTP"


def selftest():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        app, conn, db_path, token, other = setup(tmp)
        device = jl.authenticate(conn, token)
        saved = []

        def save(note, role, username):
            saved.append((note, role, username))
            return "synced"
        # 1.
        refused(lambda: jl.draft_note(conn, device, "Giulia Santoro", WORDS), "auth_required")
        last = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
        assert last["action"] == "jarvis_note_draft" and last["allowed"] == 0, "1: the refusal is audited"
        assert "SNTGLI51R49D612Y" not in (last["target"] or ""), "1: HARD FAIL - a refusal names a patient"
        assert not saved, "1: nothing saved"
        # 2.
        delegate(app, db_path, conn, token, D)
        d = jl.draft_note(conn, device, "giulia santoro", WORDS)
        assert [c["cf"] for c in d["choices"]] == ["SNTGLI51R49D612Y"] and d["text"] == WORDS, f"2: one match ({d})"
        out = jl.commit_note(conn, device, d["draft"], "SNTGLI51R49D612Y", save)
        note, role, username = saved[-1]
        assert out["saved"] and out["name"] == "Giulia Santoro", f"2: saved ({out})"
        assert note.codice_fiscale == "SNTGLI51R49D612Y" and WORDS in note.clinical_notes \
            and note.clinical_notes.startswith("Dictated to Jarvis:"), f"2: the spoken words, marked ({note})"
        assert note.procedures == [] and note.invoices == [], "2: HARD FAIL - nothing invented beside the words"
        assert (role, username) == ("dentist", "drossi"), "2: saved as the delegated dentist, under their own role"
        import patient_id
        row = conn.execute("SELECT * FROM audit_log WHERE action = 'jarvis_note' ORDER BY id DESC LIMIT 1").fetchone()
        assert row and row["username"] == "drossi" and row["allowed"] == 1 and row["reason"] == "jarvis_voice" \
            and row["target"] == patient_id.resolve(conn, "SNTGLI51R49D612Y"), \
            "2: audited as a Jarvis voice note, the patient by surrogate id (never the codice fiscale)"
        draft_row = conn.execute("SELECT * FROM audit_log WHERE action = 'jarvis_note_draft' AND allowed = 1"
                                 " ORDER BY id DESC LIMIT 1").fetchone()
        assert draft_row["target"] == f"jarvis_device:{device['id']}" and draft_row["reason"] == "one_match", \
            "2: the draft names the device, never the patient"
        # 3.
        d = jl.draft_note(conn, device, "Paola Rossi", WORDS)
        assert sorted(c["cf"] for c in d["choices"]) == ["RSPS850010150900", "RSSP850010150900"], \
            "3: HARD FAIL - same-name patients not both offered"
        refused(lambda: jl.draft_note(conn, device, "Nobody Here", WORDS), "no_match")
        refused(lambda: jl.draft_note(conn, device, "Giulia Rossi", WORDS), "no_match")    # every word must match
        refused(lambda: jl.draft_note(conn, device, "Giulia Santoro", "   "), "empty")
        old, jl.MAX_MATCHES = jl.MAX_MATCHES, 1
        try:
            refused(lambda: jl.draft_note(conn, device, "Paola", WORDS), "too_many")
        finally:
            jl.MAX_MATCHES = old
        # 4.
        n = len(saved)
        refused(lambda: jl.commit_note(conn, device, d["draft"], "GLLMRC67A11F839U", save), "patient")
        refused(lambda: jl.commit_note(conn, device, d["draft"], "RSSP850010150900", save), "gone")
        assert len(saved) == n, "4: HARD FAIL - a patient outside the matches was written, or a draft used twice"
        d = jl.draft_note(conn, device, "Marco Gallo", WORDS)
        refused(lambda: jl.commit_note(conn, jl.authenticate(conn, other), d["draft"], "GLLMRC67A11F839U", save), "gone")
        d = jl.draft_note(conn, device, "Marco Gallo", WORDS)
        late = clinic_time.now_utc() + timedelta(seconds=jl.NOTE_SECONDS + 1)
        refused(lambda: jl.commit_note(conn, device, d["draft"], "GLLMRC67A11F839U", save, now=late), "gone")
        assert len(saved) == n, "4: HARD FAIL - an expired or another device's draft was saved"
        # 5.
        d = jl.draft_note(conn, device, "Marco Gallo", WORDS)
        conn.execute("UPDATE jarvis_delegations SET revoked_at = ?, revoked_by = 'drossi'",
                     (clinic_time.to_storage(clinic_time.now_utc()),))
        conn.commit()
        refused(lambda: jl.commit_note(conn, device, d["draft"], "GLLMRC67A11F839U", save), "auth_required")
        assert len(saved) == n, "5: HARD FAIL - saved after the delegation ended"
        # only a doctor may let Jarvis write (owner, 2026-10-11) - reception may add notes in the clinic app, not here
        delegate(app, db_path, conn, token, A)
        refused(lambda: jl.draft_note(conn, device, "Marco Gallo", WORDS), "role")
        import auth
        auth.PERMISSIONS["dentist"].discard("append_note")    # and a doctor's role must still allow notes
        try:
            delegate(app, db_path, conn, token, D)
            refused(lambda: jl.draft_note(conn, device, "Marco Gallo", WORDS), "role")
        finally:
            auth.PERMISSIONS["dentist"].add("append_note")
        conn.execute("UPDATE jarvis_delegations SET revoked_at = ?, revoked_by = 'aassist' WHERE revoked_at IS NULL",
                     (clinic_time.to_storage(clinic_time.now_utc()),))
        conn.commit()
        # 6.
        anon = app.test_client()
        headers = {"Authorization": f"Bearer {token}"}
        r = anon.post("/api/jarvis/notes/draft", json={"patient": "Giulia Santoro", "text": WORDS}, headers=headers)
        body = r.get_json() or {}
        assert r.status_code == 403 and body.get("error") == "auth_required" and "choices" not in body, \
            f"6: no delegation, nothing disclosed ({r.status_code})"
        assert anon.post("/api/jarvis/notes/draft", json={"patient": "x", "text": "y"}).status_code == 401
        delegate(app, db_path, conn, token, A)                    # owner, 2026-10-11: doctors only
        r = anon.post("/api/jarvis/notes/draft", json={"patient": "Giulia Santoro", "text": WORDS}, headers=headers)
        assert r.status_code == 403 and r.get_json()["error"] == "role", "6: HARD FAIL - reception wrote through Jarvis"
        r = anon.post("/api/jarvis/login", json={"username": "drossi", "password": PASSWORD}, headers=headers)
        assert r.status_code == 200 and r.get_json()["username"] == "drossi", f"6: a doctor signs Jarvis in ({r.data})"
        r = anon.post("/api/jarvis/notes/draft", json={"patient": "Giulia Santoro", "text": WORDS}, headers=headers)
        body = r.get_json()
        assert r.status_code == 200 and body["choices"] == [{"cf": "SNTGLI51R49D612Y", "name": "Giulia Santoro"}], body
        before = len(visits(conn, "SNTGLI51R49D612Y"))
        r = anon.post("/api/jarvis/notes/commit", json={"draft": body["draft"], "cf": "SNTGLI51R49D612Y"},
                      headers=headers)
        assert r.status_code == 200 and r.get_json()["saved"], f"6: saved over HTTP ({r.get_json()})"
        notes = [v["clinical_notes"] for v in visits(conn, "SNTGLI51R49D612Y")]
        assert len(notes) == before + 1 and any(WORDS in (x or "") for x in notes), "6: the note is in the record"
        r = anon.post("/api/jarvis/notes/commit", json={"draft": body["draft"], "cf": "SNTGLI51R49D612Y"},
                      headers=headers)
        assert r.status_code == 410, "6: HARD FAIL - the same draft saved twice over HTTP"
        audit = conn.execute("SELECT COUNT(*) FROM audit_log WHERE action IN ('jarvis_note', 'jarvis_note_draft')"
                             " AND username = 'drossi' AND allowed = 1").fetchone()[0]
        assert audit >= 2, "6: the HTTP calls are audited under the delegated person"
        signin(app, conn, token, other)
        conn.close()
    print(json.dumps({"jarvis_notes_selftest": "ok"}))


if __name__ == "__main__":
    selftest()
    sys.exit(0)
