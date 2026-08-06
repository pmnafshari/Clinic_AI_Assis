import re
import sqlite3
import sys
import tempfile
from pathlib import Path

from werkzeug.security import generate_password_hash

import app.agent_routes as agent_routes
import app.db as app_db
import app.patients_routes as patients_routes
from app import create_app
from auth import authorize
from dental_notes_schema import DentalNote
from web_session import _hash_token


def _seed_user(db_path, username, password, role):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO users (username, password_hash, role, active) VALUES (?, ?, ?, 1)",
        (username, generate_password_hash(password), role),
    )
    conn.commit()
    conn.close()


def _csrf_from(html):
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    return match.group(1)


def _token_from(html):
    match = re.search(r'name="token" value="([^"]+)"', html)
    return match.group(1)


def _login(app, username, password):
    client = app.test_client()
    get_resp = client.get("/login")
    csrf = _csrf_from(get_resp.text)
    login_resp = client.post(
        "/login", data={"username": username, "password": password, "csrf_token": csrf}
    )
    assert login_resp.status_code == 302, f"login for {username} should redirect"
    return client


def _phone(db_path, cf):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT phone FROM patients WHERE codice_fiscale = ?", (cf,)).fetchone()
    conn.close()
    return row["phone"]


def selftest():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "clinic.sqlite")
        app_db.DB_PATH = db_path
        app_db.CHROMA_PATH = str(Path(tmp) / "chroma")
        agent_routes.UNDO_LOG = str(Path(tmp) / "undo_log.jsonl")

        app = create_app()
        app.config["TESTING"] = True

        # 1. the app boots clean with the vendored shell in place
        client = app.test_client()

        # 2. default-deny holds for /patients before login. patients_bp
        # doesn't exist yet (lands in plan 10.1-03) so this is a 404 today;
        # once the route is registered it becomes a 302 to /login. either
        # way an unauthenticated request must never reach patient data.
        deny_resp = client.get("/patients")
        assert deny_resp.status_code in (302, 404), \
            "unauthenticated /patients must never return 200"
        if deny_resp.status_code == 302:
            assert "/login" in deny_resp.headers["Location"], \
                "a redirect from /patients must target /login"

        # 3. vendored htmx serves straight off /static - no CDN round trip
        htmx_resp = client.get("/static/vendor/htmx/1.9.12/htmx.min.js")
        assert htmx_resp.status_code == 200, "vendored htmx.min.js should serve 200"

    print("selftest ok")

    # -----------------------------------------------------------------
    # 10.1-03 / 10.1-04 append here: once patients_bp exists, add
    # list_view/search_fragment/detail_view assertions - RBAC (dentist
    # vs assistant vs admin), fuzzy search candidates, CSRF on the edit
    # modal, confirm-diff swap.
    # -----------------------------------------------------------------

    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "clinic.sqlite")
        app_db.DB_PATH = db_path
        app_db.CHROMA_PATH = str(Path(tmp) / "chroma")
        agent_routes.UNDO_LOG = str(Path(tmp) / "undo_log.jsonl")

        app = create_app()
        app.config["TESTING"] = True

        # point the file listing at a throwaway tree so the Files section has
        # something real to leak
        sorted_root = Path(tmp) / "sorted"
        patients_routes.SORTED_ROOT = sorted_root
        # confirm_change reads/writes through sorted_root too - without this
        # /agent/confirm resolves "sorted/" against the repo, not the temp tree
        agent_routes.SORTED_ROOT = sorted_root

        _seed_user(db_path, "drossi", "goodpass", "dentist")
        _seed_user(db_path, "aassist", "goodpass", "assistant")
        _seed_user(db_path, "aadmin", "goodpass", "admin")

        cf = "RSSM800010150100"
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO patients (codice_fiscale, patient_name, phone) VALUES (?, ?, ?)",
            (cf, "mario rossi", "333123456"),
        )
        conn.execute(
            "INSERT INTO visits"
            " (codice_fiscale, visit_date, procedures, clinical_notes, next_appointment, source_path)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (cf, "2026-06-01", '["rct 26"]', "rct done on tooth 26", "2026-08-01", "n1.json"),
        )
        # an older visit inserted later - a bulk re-import assigns ids by
        # filename, so the highest id is not the latest visit
        conn.execute(
            "INSERT INTO visits"
            " (codice_fiscale, visit_date, procedures, clinical_notes, next_appointment, source_path)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (cf, "2026-03-15", "[]", "cleaning", None, "n10.json"),
        )
        # capitalized as the web form stores it - the search box must find it
        cap_cf = "GLLA920010150500"
        conn.execute(
            "INSERT INTO patients (codice_fiscale, patient_name, phone) VALUES (?, ?, ?)",
            (cap_cf, "Anna Gialli", "333999888"),
        )
        conn.commit()
        conn.close()

        (sorted_root / cf / "notes").mkdir(parents=True)
        # a real serialized note, not the "{}" placeholder - build_pending_action's
        # D-09 check opens and validates it as a DentalNote
        seed_note = DentalNote(
            patient_name="mario rossi",
            codice_fiscale=cf,
            phone="333123456",
            visit_date="2026-06-01",
            procedures=["rct 26"],
            clinical_notes="rct done on tooth 26",
            next_appointment="2026-08-01",
        )
        (sorted_root / cf / "notes" / "n1.json").write_text(seed_note.model_dump_json())
        (sorted_root / cf / "images").mkdir(parents=True)
        (sorted_root / cf / "images" / "xray-26-rct.jpg").write_text("x")

        # 1. RBAC-04 - admin gets no table, just the redirect + denied audit row
        admin_client = _login(app, "aadmin", "goodpass")
        admin_list_resp = admin_client.get("/patients")
        assert admin_list_resp.status_code == 302, \
            "admin GET /patients should redirect (RBAC-04)"
        assert "mario rossi" not in admin_list_resp.text, \
            "admin must never see the patient table"

        admin_detail_resp = admin_client.get(f"/patients/{cf}")
        assert admin_detail_resp.status_code == 302, \
            "admin GET /patients/<cf> should redirect (RBAC-04)"

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        admin_denied_rows = conn.execute(
            "SELECT * FROM audit_log WHERE username = 'aadmin' AND action = 'read_notes' AND allowed = 0"
        ).fetchall()
        conn.close()
        assert len(admin_denied_rows) == 2, \
            f"RBAC-04: expected 2 denied read_notes rows for admin, got {len(admin_denied_rows)}"

        # 2. RBAC-03 - the clinical card is present for dentist, entirely
        # absent (raw bytes, not CSS) for assistant
        dentist_client = _login(app, "drossi", "goodpass")
        dentist_detail_resp = dentist_client.get(f"/patients/{cf}")
        assert dentist_detail_resp.status_code == 200
        assert "rct done on tooth 26" in dentist_detail_resp.text, \
            "dentist (read_clinical) should see the clinical card"

        assistant_client = _login(app, "aassist", "goodpass")
        assistant_detail_resp = assistant_client.get(f"/patients/{cf}")
        assert assistant_detail_resp.status_code == 200
        assert "mario rossi" in assistant_detail_resp.text, \
            "assistant should still see the CRM card"
        assert "rct done on tooth 26" not in assistant_detail_resp.text, \
            "assistant (read_notes but not read_clinical) must not see clinical text - RBAC-03"

        # 2b. CR-01 - the file listing is clinical data too. it now lives
        # behind its own polled fragment route (files_fragment), not inline
        # on the detail page - the dentist's page just wires up the poll
        # target, and the assistant's page doesn't even mention the
        # endpoint. the filename assertions themselves moved to section 14,
        # against the fragment route.
        assert f"/patients/{cf}/files" in dentist_detail_resp.text, \
            "dentist (read_clinical) should see the files poll target"
        assert "n1.json" not in assistant_detail_resp.text, \
            "assistant must not see clinical filenames - RBAC-03"
        assert "xray-26-rct.jpg" not in assistant_detail_resp.text, \
            "assistant must not see clinical filenames - RBAC-03"
        assert f"/patients/{cf}/files" not in assistant_detail_resp.text, \
            "assistant must not even be told the files fragment endpoint exists"

        # 2c. CR-04 - the list picks next appointment / last visit by
        # visit_date, not by insert order
        dentist_list_resp = dentist_client.get("/patients")
        assert dentist_list_resp.status_code == 200
        assert "2026-08-01" in dentist_list_resp.text, \
            "next appointment must come from the newest visit by date, not the highest id"
        assert "2026-06-01" in dentist_list_resp.text, \
            "last visit must be the newest visit by date"
        assert "2026-03-15" not in dentist_list_resp.text, \
            "the older visit must not be reported as the last visit"

        # 3. SC2 - fuzzy search returns the seeded candidate for a typo,
        # and the neutral no-match copy for a miss
        typo_resp = dentist_client.get("/patients/search?q=rosi")
        assert typo_resp.status_code == 200
        assert "mario rossi" in typo_resp.text, \
            "typo query 'rosi' should surface the seeded patient"

        miss_resp = dentist_client.get("/patients/search?q=zzzzzz")
        assert miss_resp.status_code == 200
        assert "No patients match" in miss_resp.text, \
            "a query with no matches should render the neutral no-match copy"

        # 3b. CR-02 - a capitalized stored name is reachable from the search box
        for q in ("gia", "Gia", "GIA"):
            cap_resp = dentist_client.get(f"/patients/search?q={q}")
            assert cap_resp.status_code == 200
            assert "Anna Gialli" in cap_resp.text, \
                f"search '{q}' should surface the capitalized patient name"

        assistant_typo_resp = assistant_client.get("/patients/search?q=rosi")
        assert assistant_typo_resp.status_code == 200
        assert "mario rossi" in assistant_typo_resp.text, \
            "assistant (holds read_notes) should also reach the search fragment"

        # 4. both list and detail as admin blocked (search fragment too)
        admin_search_resp = admin_client.get("/patients/search?q=rosi")
        assert admin_search_resp.status_code == 403, \
            "admin should get a bare 403 from the search fragment, not a redirect"

        # 5. modal edit -> confirm-diff -> apply (GUI-06 SC3, D-07). The
        # edit-form fragment is chrome-free - loads straight into the
        # shared #modal-body-target, no page reload.
        edit_form_resp = dentist_client.get(f"/patients/{cf}/edit-form?field=phone")
        assert edit_form_resp.status_code == 200
        assert 'name="value"' in edit_form_resp.text, \
            "edit-form fragment should carry the value input"
        assert "modal-dialog" not in edit_form_resp.text and "extends" not in edit_form_resp.text, \
            "edit-form fragment must be chrome-free (no modal wrapper)"

        # 6. CSRF is enforced on the edit POST, same as every other write
        no_csrf_resp = dentist_client.post(
            f"/patients/{cf}/edit", data={"field": "phone", "value": "333-1234"}
        )
        assert no_csrf_resp.status_code == 400, \
            "edit POST without a csrf token should be rejected"
        assert _phone(db_path, cf) == "333123456", \
            "a rejected csrf edit must not touch the db"

        # 7. build swaps the modal body to the confirm-diff fragment - the
        # write is frozen but not yet applied (D-07's hard constraint)
        edit_csrf = _csrf_from(edit_form_resp.text)
        build_resp = dentist_client.post(
            f"/patients/{cf}/edit",
            data={"field": "phone", "value": "333-1234", "csrf_token": edit_csrf},
        )
        assert build_resp.status_code == 200
        assert 'name="token"' in build_resp.text, \
            "confirm-diff fragment should carry a hidden token"
        assert "333123456" in build_resp.text and "333-1234" in build_resp.text, \
            "confirm-diff fragment should show the old -> new diff"
        assert _phone(db_path, cf) == "333123456", \
            "building the diff must not write to the db until confirm"

        token = _token_from(build_resp.text)
        confirm_csrf = _csrf_from(build_resp.text)

        # 8. confirming posts to the reused agent.confirm_change endpoint -
        # the modal path never reimplements apply/write/audit
        confirm_resp = dentist_client.post(
            "/agent/confirm", data={"token": token, "csrf_token": confirm_csrf}
        )
        assert confirm_resp.status_code == 302, "confirm should redirect on success"
        assert _phone(db_path, cf) == "333-1234", \
            "confirming should apply the frozen new value"

        # 9. RBAC re-check (SC4) - build as dentist (authorized), then
        # downgrade the live session's role before confirming. The denial
        # must come from the independent check inside apply, not the UI.
        edit_form_resp2 = dentist_client.get(f"/patients/{cf}/edit-form?field=phone")
        edit_csrf2 = _csrf_from(edit_form_resp2.text)
        build_resp2 = dentist_client.post(
            f"/patients/{cf}/edit",
            data={"field": "phone", "value": "555-0000", "csrf_token": edit_csrf2},
        )
        token2 = _token_from(build_resp2.text)
        confirm_csrf2 = _csrf_from(build_resp2.text)

        raw_session_token = dentist_client.get_cookie("session_token").value
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE sessions SET role = 'assistant' WHERE token_hash = ?",
            (_hash_token(raw_session_token),),
        )
        conn.commit()
        conn.close()

        denied_resp = dentist_client.post(
            "/agent/confirm", data={"token": token2, "csrf_token": confirm_csrf2}
        )
        assert denied_resp.status_code == 200, "a denied confirm should re-render, not redirect"
        assert "permission" in denied_resp.text.lower(), \
            "a denied confirm should show a permission message"
        assert _phone(db_path, cf) == "333-1234", \
            "RBAC re-check: a denied confirm must not change the phone"

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        denied_rows = conn.execute(
            "SELECT * FROM audit_log WHERE action = 'update_field' AND allowed = 0"
        ).fetchall()
        conn.close()
        assert len(denied_rows) == 1, \
            f"RBAC re-check: expected 1 denied update_field row, got {len(denied_rows)}"

        # restore the live session's role for any assertions added later
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE sessions SET role = 'dentist' WHERE token_hash = ?",
            (_hash_token(raw_session_token),),
        )
        conn.commit()
        conn.close()

        # 10. CR-03 - the modal confirm goes through htmx, which follows a 302
        # on its xhr and would swap the whole dashboard document into the modal
        # body. an HX-Request must get a fragment plus HX-Redirect instead.
        edit_form_resp3 = dentist_client.get(f"/patients/{cf}/edit-form?field=phone")
        build_resp3 = dentist_client.post(
            f"/patients/{cf}/edit",
            data={
                "field": "phone",
                "value": "333-5678",
                "csrf_token": _csrf_from(edit_form_resp3.text),
            },
        )
        confirm_csrf3 = _csrf_from(build_resp3.text)
        htmx_confirm_resp = dentist_client.post(
            "/agent/confirm",
            data={"token": _token_from(build_resp3.text), "csrf_token": confirm_csrf3},
            headers={"HX-Request": "true"},
        )
        assert htmx_confirm_resp.status_code == 200, \
            "an htmx confirm must not answer 302 - the xhr follows it silently"
        assert "<html" not in htmx_confirm_resp.text.lower(), \
            "an htmx confirm must not swap a full page into the modal body"
        assert cf in htmx_confirm_resp.headers.get("HX-Redirect", ""), \
            "an htmx confirm should send the browser back to the patient page"
        assert _phone(db_path, cf) == "333-5678", \
            "the htmx confirm should still apply the frozen value"

        # the error branches are fragments too, not agent_confirm.html
        stale_confirm_resp = dentist_client.post(
            "/agent/confirm",
            data={"token": "nosuchtoken", "csrf_token": confirm_csrf3},
            headers={"HX-Request": "true"},
        )
        assert stale_confirm_resp.status_code == 200
        assert "<html" not in stale_confirm_resp.text.lower(), \
            "an htmx confirm error must stay a fragment too"
        assert "expired" in stale_confirm_resp.text.lower(), \
            "a stale token should say so inside the modal"

        # 11. the visit-edit fragment - its gate and its ownership check (D-06/D-11)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        visit_id = conn.execute(
            "SELECT id FROM visits WHERE source_path = 'n1.json'"
        ).fetchone()["id"]
        other_visit_id = conn.execute(
            "SELECT id FROM visits WHERE source_path = 'n10.json'"
        ).fetchone()["id"]
        conn.close()

        visit_fragment_resp = dentist_client.get(f"/patients/{cf}/visits/{visit_id}/edit-form")
        assert visit_fragment_resp.status_code == 200
        assert "2026-08-01" in visit_fragment_resp.text, \
            "visit-edit fragment should show the current next appointment"
        assert "2026-06-01" in visit_fragment_resp.text, \
            "visit-edit fragment should say which visit is being edited"
        assert "<html" not in visit_fragment_resp.text.lower(), \
            "visit-edit fragment must be chrome-free (loads into the modal)"

        assistant_fragment_resp = assistant_client.get(f"/patients/{cf}/visits/{visit_id}/edit-form")
        assert assistant_fragment_resp.status_code == 403, \
            "assistant should get a bare 403 from the visit-edit fragment (D-11)"
        assert assistant_fragment_resp.text == "", \
            "a denied fragment must be empty, not a page"
        assert "Location" not in assistant_fragment_resp.headers, \
            "a denied fragment must never redirect"

        foreign_resp = dentist_client.get(f"/patients/{cap_cf}/visits/{other_visit_id}/edit-form")
        assert foreign_resp.status_code == 404, \
            "a visit id from another patient's record must 404 (D-06)"

        missing_resp = dentist_client.get(f"/patients/{cf}/visits/999999/edit-form")
        assert missing_resp.status_code == 404, "an unknown visit id must 404"

        # 12. build, confirm, apply (GUI-08's ROADMAP success criterion 2)
        visit_edit_csrf = _csrf_from(visit_fragment_resp.text)
        visit_build_resp = dentist_client.post(
            f"/patients/{cf}/visits/{visit_id}/edit",
            data={"value": "2026-09-10", "csrf_token": visit_edit_csrf},
        )
        assert visit_build_resp.status_code == 200
        assert "<html" not in visit_build_resp.text.lower()
        assert "next_appointment:" in visit_build_resp.text
        assert "2026-08-01" in visit_build_resp.text and "2026-09-10" in visit_build_resp.text
        assert "mario rossi" in visit_build_resp.text
        assert "visit of" in visit_build_resp.text.lower()
        assert 'name="token"' in visit_build_resp.text

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        next_appt = conn.execute(
            "SELECT next_appointment FROM visits WHERE id = ?", (visit_id,)
        ).fetchone()["next_appointment"]
        conn.close()
        assert next_appt == "2026-08-01", "building the diff must not write the visit row"

        visit_token = _token_from(visit_build_resp.text)
        visit_confirm_csrf = _csrf_from(visit_build_resp.text)
        visit_confirm_resp = dentist_client.post(
            "/agent/confirm",
            data={"token": visit_token, "csrf_token": visit_confirm_csrf},
            headers={"HX-Request": "true"},
        )
        assert visit_confirm_resp.status_code == 200
        assert "<html" not in visit_confirm_resp.text.lower()
        assert cf in visit_confirm_resp.headers.get("HX-Redirect", ""), \
            "confirming a visit edit should send the browser back to the patient page"

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        next_appt = conn.execute(
            "SELECT next_appointment FROM visits WHERE id = ?", (visit_id,)
        ).fetchone()["next_appointment"]
        conn.close()
        assert next_appt == "2026-09-10", "confirming should apply the frozen new value to sqlite"

        note_json = DentalNote.model_validate_json(
            (sorted_root / cf / "notes" / "n1.json").read_text()
        )
        assert note_json.next_appointment == "2026-09-10", \
            "confirming should apply the frozen new value to the json sibling too (D-09)"
        assert note_json.clinical_notes == "rct done on tooth 26", \
            "a visit-field edit must never touch clinical_notes"

        # assistant's own csrf, not the dentist's - a fragment gate is checked
        # after csrf, so the wrong session's token would 400 before it 403s
        assistant_csrf = _csrf_from(assistant_client.get(f"/patients/{cf}").text)
        assistant_visit_edit_resp = assistant_client.post(
            f"/patients/{cf}/visits/{visit_id}/edit",
            data={"value": "2026-10-10", "csrf_token": assistant_csrf},
        )
        assert assistant_visit_edit_resp.status_code == 403, \
            "assistant should get a bare 403 from the visit-edit submit (D-11)"

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        next_appt = conn.execute(
            "SELECT next_appointment FROM visits WHERE id = ?", (visit_id,)
        ).fetchone()["next_appointment"]
        conn.close()
        assert next_appt == "2026-09-10", "a denied submit must not change the visit row"

        clear_form_resp = dentist_client.get(f"/patients/{cf}/visits/{visit_id}/edit-form")
        clear_csrf = _csrf_from(clear_form_resp.text)
        clear_build_resp = dentist_client.post(
            f"/patients/{cf}/visits/{visit_id}/edit",
            data={"value": "", "csrf_token": clear_csrf},
        )
        clear_token = _token_from(clear_build_resp.text)
        clear_confirm_csrf = _csrf_from(clear_build_resp.text)
        dentist_client.post(
            "/agent/confirm",
            data={"token": clear_token, "csrf_token": clear_confirm_csrf},
            headers={"HX-Request": "true"},
        )

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        next_appt = conn.execute(
            "SELECT next_appointment FROM visits WHERE id = ?", (visit_id,)
        ).fetchone()["next_appointment"]
        conn.close()
        assert next_appt is None, "an empty value should clear next_appointment to NULL (D-07)"
        note_json = DentalNote.model_validate_json(
            (sorted_root / cf / "notes" / "n1.json").read_text()
        )
        assert note_json.next_appointment is None, "an empty value should clear the json field too"

        # set it back so section 13 has a value to fail to change
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE visits SET next_appointment = '2026-09-10' WHERE id = ?", (visit_id,)
        )
        conn.commit()
        conn.close()
        note_json.next_appointment = "2026-09-10"
        (sorted_root / cf / "notes" / "n1.json").write_text(note_json.model_dump_json())

        # 13. SC4 RBAC re-check on the new tool - a role downgraded mid-flow
        # is denied at apply time, not just hidden by the UI
        recheck_form_resp = dentist_client.get(f"/patients/{cf}/visits/{visit_id}/edit-form")
        recheck_csrf = _csrf_from(recheck_form_resp.text)
        recheck_build_resp = dentist_client.post(
            f"/patients/{cf}/visits/{visit_id}/edit",
            data={"value": "2026-12-25", "csrf_token": recheck_csrf},
        )
        recheck_token = _token_from(recheck_build_resp.text)
        recheck_confirm_csrf = _csrf_from(recheck_build_resp.text)

        raw_session_token2 = dentist_client.get_cookie("session_token").value
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE sessions SET role = 'assistant' WHERE token_hash = ?",
            (_hash_token(raw_session_token2),),
        )
        conn.commit()
        conn.close()

        recheck_denied_resp = dentist_client.post(
            "/agent/confirm",
            data={"token": recheck_token, "csrf_token": recheck_confirm_csrf},
        )
        assert recheck_denied_resp.status_code == 200, \
            "a denied visit-field confirm should re-render, not redirect"
        assert "permission" in recheck_denied_resp.text.lower(), \
            "a denied visit-field confirm should show a permission message"

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        next_appt = conn.execute(
            "SELECT next_appointment FROM visits WHERE id = ?", (visit_id,)
        ).fetchone()["next_appointment"]
        denied_visit_rows = conn.execute(
            "SELECT * FROM audit_log WHERE action = 'update_visit_field' AND allowed = 0"
        ).fetchall()
        conn.close()
        assert next_appt == "2026-09-10", \
            "a mid-flow role downgrade must not change the visit row"
        assert len(denied_visit_rows) == 1, \
            f"expected 1 denied update_visit_field row, got {len(denied_visit_rows)}"

        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE sessions SET role = 'dentist' WHERE token_hash = ?",
            (_hash_token(raw_session_token2),),
        )
        conn.commit()
        conn.close()

        # 14. the Files fragment (GUI-09, D-13/D-15) - dentist-only, CF-validated,
        # and re-derived per request so a late-arriving file shows up on the next
        # fetch rather than only at page-render time
        files_resp = dentist_client.get(f"/patients/{cf}/files")
        assert files_resp.status_code == 200
        assert "n1.json" in files_resp.text, \
            "the fragment must still list the .json sibling - D-15, nothing filtered"
        assert "xray-26-rct.jpg" in files_resp.text
        assert "<html" not in files_resp.text.lower(), \
            "the files fragment must be chrome-free - safe for an htmx swap"

        assistant_files_resp = assistant_client.get(f"/patients/{cf}/files")
        assert assistant_files_resp.status_code == 403, \
            "assistant should get a bare 403 from the files fragment (CR-01/RBAC-03)"
        assert assistant_files_resp.text == "", \
            "a denied files fragment must be empty, not a page"
        assert "Location" not in assistant_files_resp.headers, \
            "a denied files fragment must never redirect"

        admin_files_resp = admin_client.get(f"/patients/{cf}/files")
        assert admin_files_resp.status_code == 403, \
            "admin should also get a bare 403 from the files fragment"

        bad_cf_resp = dentist_client.get("/patients/NOTACF/files")
        assert bad_cf_resp.status_code == 404, \
            "a malformed cf must 404 before any filesystem access (T-06-02)"

        empty_files_resp = dentist_client.get(f"/patients/{cap_cf}/files")
        assert empty_files_resp.status_code == 200
        assert "No files on record." in empty_files_resp.text, \
            "a patient with no sorted directory should render the empty state"

        # the poll's whole point: a file that lands after the first fetch
        # must appear on the next one, with no page reload in between
        (sorted_root / cf / "images" / "xray-36-new.jpg").write_text("x")
        refetch_resp = dentist_client.get(f"/patients/{cf}/files")
        assert refetch_resp.status_code == 200
        assert "xray-36-new.jpg" in refetch_resp.text, \
            "a file that arrives after the first fetch must show up on the next poll"

        # 15. the header Add note button (GUI-07, D-01) - in the page header,
        # not the dentist-only clinical card, since assistants hold
        # append_note too
        dentist_notes_link_resp = dentist_client.get(f"/patients/{cf}")
        assert f"/notes/new?cf={cf}" in dentist_notes_link_resp.text, \
            "dentist should see the Add note link, scoped to this patient"
        assert 'hx-trigger="load, every 5s"' in dentist_notes_link_resp.text, \
            "dentist's page should poll the Files fragment"

        assistant_notes_link_resp = assistant_client.get(f"/patients/{cf}")
        assert f"/notes/new?cf={cf}" in assistant_notes_link_resp.text, \
            "assistant holds append_note too - D-01 puts the button outside the clinical card"
        assert 'hx-trigger="load, every 5s"' not in assistant_notes_link_resp.text, \
            "assistant lacks read_clinical, so the Files poll div must not be on their page"

        # 16. CHAT-03 SC1 - staff issue a patient PIN, and it is shown once
        import re as _re

        import patient_auth

        def _cred_count(codice):
            c = sqlite3.connect(db_path)
            n = c.execute(
                "SELECT COUNT(*) FROM patient_credentials WHERE codice_fiscale = ?", (codice,)
            ).fetchone()[0]
            c.close()
            return n

        def _issue(client, codice):
            # the control is a bare hx-post button, so the token rides in the
            # X-CSRFToken header that base.html's hx-headers sets - the same
            # path a browser uses. the token is sourced from a page every role
            # can reach: admin's patient-detail GET redirects under RBAC-04, so
            # reading it from there would fail csrf before the authorize gate
            # and mask what this is testing.
            page = client.get("/", follow_redirects=True)
            return client.post(
                f"/patients/{codice}/issue-pin",
                headers={"X-CSRFToken": _csrf_from(page.text)},
            )

        # csrf is enforced here too
        assert dentist_client.post(f"/patients/{cf}/issue-pin").status_code == 400, \
            "16: issue-pin without a csrf token should be rejected"
        assert _cred_count(cf) == 0, "16: a rejected csrf issue must write nothing"

        issue_resp = _issue(dentist_client, cf)
        assert issue_resp.status_code == 200, "16: a dentist should be able to issue a PIN"
        pin_match = _re.search(r"\b(\d{8})\b", issue_resp.text)
        assert pin_match, "16: the response should display an 8-digit PIN"
        issued_pin = pin_match.group(1)
        assert _cred_count(cf) == 1, "16: a credential row should exist"

        # the assistant holds it too (D-01)
        assistant_issue_resp = _issue(assistant_client, cap_cf)
        assert assistant_issue_resp.status_code == 200, "16: an assistant should be able to issue"

        # 17. admin cannot, and the denial is audited
        before_admin = _cred_count(cf)
        admin_issue_resp = _issue(admin_client, cf)
        assert admin_issue_resp.status_code == 403, "17: admin must not be able to issue a PIN"
        assert _cred_count(cf) == before_admin, "17: a denied issue must write nothing"
        conn_a = sqlite3.connect(db_path)
        denied = conn_a.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action = 'issue_patient_pin' AND allowed = 0"
        ).fetchone()[0]
        conn_a.close()
        assert denied == 1, f"17: expected 1 denied issue_patient_pin row, got {denied}"

        # 18. D-02 show-once - the PIN is in that one response body and nowhere
        # else. hashing is necessary but does not stop a leak through the page
        # or the audit log, which is where it would actually land.
        detail_after = dentist_client.get(f"/patients/{cf}")
        assert issued_pin not in detail_after.text, \
            "18: the PIN must not be retrievable from the patient page"
        conn_b = sqlite3.connect(db_path)
        leaked = conn_b.execute(
            "SELECT COUNT(*) FROM audit_log WHERE target = ? OR username = ?",
            (issued_pin, issued_pin),
        ).fetchone()[0]
        conn_b.close()
        assert leaked == 0, "18: the PIN must never appear in the audit log"

        # 19. reissue supersedes the previous credential
        reissue_resp = _issue(dentist_client, cf)
        new_pin = _re.search(r"\b(\d{8})\b", reissue_resp.text).group(1)
        assert new_pin != issued_pin, "19: reissue should produce a different PIN"
        conn_c = sqlite3.connect(db_path)
        conn_c.row_factory = sqlite3.Row
        assert patient_auth.verify_pin(cf, issued_pin, conn_c)[0] == "wrong", \
            "19: the superseded PIN must stop working"
        assert patient_auth.verify_pin(cf, new_pin, conn_c)[0] == "ok", \
            "19: the new PIN must work"
        conn_c.close()
        assert _cred_count(cf) == 1, "19: reissue must update in place, not add a row"

        # 20. an unknown patient 404s and writes nothing
        unknown_cf = "ZZZZ999999999999"
        unknown_resp = _issue(dentist_client, unknown_cf)
        assert unknown_resp.status_code == 404, "20: issuing for an unknown patient should 404"
        assert _cred_count(unknown_cf) == 0, "20: no credential row for an unknown patient"

        # 21. D-11 - the gate: dentist and assistant may revoke, admin/system
        # may not (Phase 17's D-01/D-02, not re-opened here)
        assert authorize("dentist", "revoke_patient_pin"), \
            "21: dentist should allow revoke_patient_pin"
        assert authorize("assistant", "revoke_patient_pin"), \
            "21: assistant should allow revoke_patient_pin"
        assert not authorize("admin", "revoke_patient_pin"), \
            "21: admin should deny revoke_patient_pin"
        assert not authorize("system", "revoke_patient_pin"), \
            "21: system should deny revoke_patient_pin"

        # a patient of its own, seeded fresh so the revoke tests don't step on
        # the credential state sections 16-20 already built up
        revoke_cf = "VRDL900010150300"
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO patients (codice_fiscale, patient_name, phone) VALUES (?, ?, ?)",
            (revoke_cf, "luca verdi", "333555777"),
        )
        conn.commit()
        conn.close()

        def _revoke(client, codice):
            # same csrf-sourcing shape as _issue - a page every role can reach
            page = client.get("/", follow_redirects=True)
            return client.post(
                f"/patients/{codice}/revoke-pin",
                headers={"X-CSRFToken": _csrf_from(page.text)},
            )

        def _session_count(codice):
            c = sqlite3.connect(db_path)
            n = c.execute(
                "SELECT COUNT(*) FROM patient_sessions WHERE codice_fiscale = ?", (codice,)
            ).fetchone()[0]
            c.close()
            return n

        def _is_active(codice):
            c = sqlite3.connect(db_path)
            c.row_factory = sqlite3.Row
            row = c.execute(
                "SELECT active FROM patient_credentials WHERE codice_fiscale = ?", (codice,)
            ).fetchone()
            c.close()
            return row["active"]

        # 22. the route works for a dentist - a live session dies with the
        # credential it stood on
        conn = sqlite3.connect(db_path)
        revoke_pin = patient_auth.issue_pin(revoke_cf, conn, "drossi", "dentist")
        patient_auth.create_patient_session(conn, revoke_cf)
        conn.close()
        assert _session_count(revoke_cf) == 1, "22: setup should leave one live session"

        revoke_resp = _revoke(dentist_client, revoke_cf)
        assert revoke_resp.status_code == 200, "22: a dentist should be able to revoke a PIN"
        assert _is_active(revoke_cf) == 0, "22: revoking should set active = 0"
        assert _session_count(revoke_cf) == 0, \
            "22: revoking should destroy every live session for this patient"

        # 23. the assistant control works too, same shape
        assistant_revoke_cf = "BNCS910010150400"
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO patients (codice_fiscale, patient_name, phone) VALUES (?, ?, ?)",
            (assistant_revoke_cf, "sara bianchi", "333111222"),
        )
        patient_auth.issue_pin(assistant_revoke_cf, conn, "aassist", "assistant")
        patient_auth.create_patient_session(conn, assistant_revoke_cf)
        conn.close()

        assistant_revoke_resp = _revoke(assistant_client, assistant_revoke_cf)
        assert assistant_revoke_resp.status_code == 200, \
            "23: an assistant should be able to revoke a PIN"
        assert _is_active(assistant_revoke_cf) == 0, "23: revoking should set active = 0"
        assert _session_count(assistant_revoke_cf) == 0, \
            "23: revoking should destroy every live session for this patient"

        # 24. admin is refused, and the refusal is audited (RBAC-04/D-01) -
        # the 403 must come from authorize() in the route, not from hiding
        # the button
        admin_target_cf = cap_cf
        before_active = _is_active(admin_target_cf)
        admin_revoke_resp = _revoke(admin_client, admin_target_cf)
        assert admin_revoke_resp.status_code == 403, "24: admin must not be able to revoke a PIN"
        assert _is_active(admin_target_cf) == before_active, \
            "24: a denied revoke must not touch the credential"
        conn_d = sqlite3.connect(db_path)
        admin_denied = conn_d.execute(
            "SELECT COUNT(*) FROM audit_log"
            " WHERE action = 'revoke_patient_pin' AND role = 'admin' AND allowed = 0"
        ).fetchone()[0]
        conn_d.close()
        assert admin_denied == 1, \
            f"24: expected 1 denied revoke_patient_pin row for admin, got {admin_denied}"

        # 25. success is audited too
        conn_e = sqlite3.connect(db_path)
        success_rows = conn_e.execute(
            "SELECT COUNT(*) FROM audit_log"
            " WHERE action = 'revoke_patient_pin' AND allowed = 1 AND username = 'drossi'"
        ).fetchone()[0]
        conn_e.close()
        assert success_rows == 1, \
            f"25: expected 1 allowed revoke_patient_pin row for drossi, got {success_rows}"

        # 26. csrf is required, same as every other write
        no_csrf_revoke_resp = dentist_client.post(f"/patients/{cap_cf}/revoke-pin")
        assert no_csrf_revoke_resp.status_code == 400, \
            "26: revoke-pin without a csrf token should be rejected"

        # 27. an unknown patient 404s and writes nothing
        unknown_revoke_cf = "ZZZZ888888888888"
        unknown_revoke_resp = _revoke(dentist_client, unknown_revoke_cf)
        assert unknown_revoke_resp.status_code == 404, \
            "27: revoking for an unknown patient should 404"
        conn_f = sqlite3.connect(db_path)
        unknown_cred_count = conn_f.execute(
            "SELECT COUNT(*) FROM patient_credentials WHERE codice_fiscale = ?",
            (unknown_revoke_cf,),
        ).fetchone()[0]
        conn_f.close()
        assert unknown_cred_count == 0, "27: no credential row for an unknown patient"

        # 28. no distinct signal after revocation (WR-10 / T-17-51) - the
        # revoked credential reads exactly like a wrong pin: same status,
        # counted toward the lockout, one audit row. this is what proves the
        # revoke control cannot become an unthrottled, unaudited probe target.
        conn_g = sqlite3.connect(db_path)
        conn_g.row_factory = sqlite3.Row
        before_attempts = conn_g.execute(
            "SELECT failed_attempts FROM patient_credentials WHERE codice_fiscale = ?",
            (revoke_cf,),
        ).fetchone()["failed_attempts"]
        before_checks = conn_g.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action = 'patient_pin_check' AND target = ?",
            (revoke_cf,),
        ).fetchone()[0]
        status, row = patient_auth.verify_pin(revoke_cf, revoke_pin, conn_g)
        assert status == "wrong", \
            "28: a revoked credential with the correct pin must read exactly like a wrong pin"
        after_attempts = conn_g.execute(
            "SELECT failed_attempts FROM patient_credentials WHERE codice_fiscale = ?",
            (revoke_cf,),
        ).fetchone()["failed_attempts"]
        after_checks = conn_g.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action = 'patient_pin_check' AND target = ?",
            (revoke_cf,),
        ).fetchone()[0]
        conn_g.close()
        assert after_attempts == before_attempts + 1, \
            "28: probing a revoked credential must still count toward the lockout"
        assert after_checks == before_checks + 1, \
            "28: probing a revoked credential must still write a patient_pin_check row"

        # 29. the button is rendered for a dentist, absent for an admin -
        # admin never reaches the page at all (RBAC-04), so this asserts the
        # redirect and relies on section 24's 403 for the real control
        dentist_button_resp = dentist_client.get(f"/patients/{cap_cf}")
        assert "revoke-pin" in dentist_button_resp.text, \
            "29: dentist should see the revoke-pin control"

        admin_page_resp = admin_client.get(f"/patients/{cap_cf}")
        assert admin_page_resp.status_code == 302, \
            "29: admin GET /patients/<cf> should redirect (RBAC-04)"

    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    print("usage: python patients_routes_selftest.py --selftest")


if __name__ == "__main__":
    main()
