import re
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from werkzeug.security import generate_password_hash

import agent
import app.dashboard_routes as dashboard_routes
import app.db as app_db
from app import create_app
from auth import log_audit
from web_auth import MIN_PASSWORD_LENGTH
from web_session import SESSION_IDLE_MINUTES, _hash_token


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


def selftest():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "clinic.sqlite")
        app_db.DB_PATH = db_path

        app = create_app()
        app.config["TESTING"] = True

        _seed_user(db_path, "drossi", "goodpass", "dentist")

        # 1. AUTH-01 login success reaches the dashboard showing username + role
        client_a = app.test_client()
        get_resp = client_a.get("/login")
        assert get_resp.status_code == 200, "1: GET /login should return 200"
        csrf_a = _csrf_from(get_resp.text)

        login_resp = client_a.post(
            "/login",
            data={"username": "drossi", "password": "goodpass", "csrf_token": csrf_a},
        )
        assert login_resp.status_code == 302, "1: successful login should redirect"

        dash_resp = client_a.get("/")
        assert dash_resp.status_code == 200, "1: dashboard should be reachable after login"
        assert b"drossi" in dash_resp.data and b"dentist" in dash_resp.data, \
            "1: dashboard should show the logged-in username and role"

        # 2. AUTH-02 cookie flags - session_token carries HttpOnly + SameSite=Strict
        set_cookie_headers = login_resp.headers.getlist("Set-Cookie")
        session_cookie = next(h for h in set_cookie_headers if h.startswith("session_token="))
        assert "HttpOnly" in session_cookie, "2: session_token cookie must be HttpOnly"
        assert "SameSite=Strict" in session_cookie, "2: session_token cookie must be SameSite=Strict"

        # 3. AUTH-02 persistence - a later request on the same client stays authenticated
        second_resp = client_a.get("/")
        assert second_resp.status_code == 200, "3: session should persist across requests"

        # 4. AUTH-01 generic failure - wrong password gives one generic message
        client_b = app.test_client()
        get_resp_b = client_b.get("/login")
        csrf_b = _csrf_from(get_resp_b.text)
        bad_resp = client_b.post(
            "/login",
            data={"username": "drossi", "password": "wrongpass", "csrf_token": csrf_b},
        )
        assert bad_resp.status_code == 200, "4: failed login should re-render the login page"
        assert b"invalid username or password" in bad_resp.data, \
            "4: failed login should show the generic error"
        assert client_b.get_cookie("session_token") is None, \
            "4: a failed login must not issue a session cookie"

        # 5. D-05 CSRF required - a login POST without csrf_token is rejected
        client_c = app.test_client()
        client_c.get("/login")
        no_csrf_resp = client_c.post(
            "/login", data={"username": "drossi", "password": "goodpass"}
        )
        assert no_csrf_resp.status_code == 400, "5: missing csrf_token should be rejected"
        assert client_c.get_cookie("session_token") is None, \
            "5: a rejected csrf login must not issue a session cookie"

        # 6. D-08 / Pitfall 1 - logged-out default-deny, unmatched URL stays a 404
        client_d = app.test_client()
        deny_resp = client_d.get("/")
        assert deny_resp.status_code == 302, "6: logged-out GET / should redirect"
        assert "/login" in deny_resp.headers["Location"], "6: redirect target should be /login"
        missing_resp = client_d.get("/no-such-page")
        assert missing_resp.status_code == 404, "6: unmatched URL should 404, not redirect"

        # 7. AUTH-02 idle expiry - a stale session redirects to login
        client_e = app.test_client()
        get_resp_e = client_e.get("/login")
        csrf_e = _csrf_from(get_resp_e.text)
        client_e.post(
            "/login",
            data={"username": "drossi", "password": "goodpass", "csrf_token": csrf_e},
        )
        raw_token = client_e.get_cookie("session_token").value
        past = datetime.now() - timedelta(minutes=SESSION_IDLE_MINUTES + 1)
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE sessions SET last_seen_at = ? WHERE token_hash = ?",
            (past.isoformat(), _hash_token(raw_token)),
        )
        conn.commit()
        conn.close()
        idle_resp = client_e.get("/")
        assert idle_resp.status_code == 302, "7: idle-expired session should redirect"
        assert "/login" in idle_resp.headers["Location"], "7: idle redirect target should be /login"

        # 8. AUTH-02 logout invalidation - the session row is gone, next request redirects
        client_f = app.test_client()
        get_resp_f = client_f.get("/login")
        csrf_f = _csrf_from(get_resp_f.text)
        client_f.post(
            "/login",
            data={"username": "drossi", "password": "goodpass", "csrf_token": csrf_f},
        )
        logout_raw_token = client_f.get_cookie("session_token").value
        dash_resp_f = client_f.get("/")
        csrf_f2 = _csrf_from(dash_resp_f.text)
        logout_resp = client_f.post("/logout", data={"csrf_token": csrf_f2})
        assert logout_resp.status_code == 302, "8: logout should redirect"
        assert "/login" in logout_resp.headers["Location"], "8: logout should redirect to /login"

        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT 1 FROM sessions WHERE token_hash = ?", (_hash_token(logout_raw_token),)
        ).fetchone()
        conn.close()
        assert row is None, "8: destroy_session should delete the session row"

        after_logout_resp = client_f.get("/")
        assert after_logout_resp.status_code == 302, "8: a request after logout should redirect"
        assert "/login" in after_logout_resp.headers["Location"], \
            "8: post-logout redirect target should be /login"

        # 9. GUI-05/D-07 - dashboard shows only the acting user's own undo history
        _seed_user(db_path, "drossi2", "goodpass", "dentist")
        dashboard_routes.UNDO_LOG = str(Path(tmp) / "undo_log.jsonl")
        agent.write_undo_entry(
            {
                "ts": "2026-01-01T00:00:00",
                "tool": "update_field",
                "codice_fiscale": "RSSM800010150100",
                "target": "sqlite:patients.phone",
                "before": "111-1111",
                "username": "drossi",
            },
            dashboard_routes.UNDO_LOG,
        )
        agent.write_undo_entry(
            {
                "ts": "2026-01-01T00:00:01",
                "tool": "update_field",
                "codice_fiscale": "MRTLGU900010150100",
                "target": "sqlite:patients.phone",
                "before": "222-2222",
                "username": "drossi2",
            },
            dashboard_routes.UNDO_LOG,
        )

        client_g = app.test_client()
        get_resp_g = client_g.get("/login")
        csrf_g = _csrf_from(get_resp_g.text)
        client_g.post(
            "/login",
            data={"username": "drossi", "password": "goodpass", "csrf_token": csrf_g},
        )
        dash_resp_g = client_g.get("/")
        assert b"RSSM800010150100" in dash_resp_g.data, \
            "9: dashboard should show the acting user's own change"
        assert b"Undo change" in dash_resp_g.data, \
            "9: the most-recent row should carry an Undo change link"
        assert b"MRTLGU900010150100" not in dash_resp_g.data, \
            "9: another user's entry must not be shown"

        # 10. GUI-06 SC1 - no-CDN: authenticated pages fetch every asset
        # locally, no external http(s) reference in a <script>/<link> tag
        no_cdn_resp = client_a.get("/")
        assert not re.search(
            r'<(?:script|link)[^>]+(?:src|href)="https?://', no_cdn_resp.text, re.IGNORECASE
        ), "10: authenticated pages must not reference any external http(s) asset"

        # 10a. UX-01/UX-02 - the shared token layer resolves end to end. a
        # <link> to a 404 is indistinguishable from a working one in markup,
        # so request the file and look for a token in the body. served
        # without a session because the login page needs it, same as /static.
        tokens_resp = app.test_client().get("/shared/css/tokens.css")
        assert tokens_resp.status_code == 200, \
            "10a: the shared token stylesheet must serve without a session"
        assert b"--ds-primary" in tokens_resp.data, \
            "10a: the shared stylesheet must actually carry the design tokens"
        assert app.test_client().get("/shared/fonts/InterVariable.woff2").status_code == 200, \
            "10a: the vendored typeface must serve locally"
        assert b"shared/css/tokens.css" in no_cdn_resp.data, \
            "10a: an authenticated page must load the shared token layer"

        # 10b. the whitelist grew by exactly one named endpoint. a prefix
        # match here would open every route starting with the same letters.
        from app import WHITELIST_ENDPOINTS
        assert WHITELIST_ENDPOINTS == {"static", "shared", "auth.login"}, \
            "10b: the no-session whitelist must hold exactly static, shared and login"
        assert app.test_client().get("/patients").status_code == 302, \
            "10b: every other route must still redirect to login without a session"

        # 10b2. the appointments surface is behind the session gate like every
        # other route, and the dashboard's agenda card obeys the same
        # withhold rule as its clinical figures: a role without
        # manage_appointments gets a dashboard with no agenda markup in the
        # body, not one where the card is hidden.
        assert app.test_client().get("/appointments").status_code == 302, \
            "10b2: /appointments must redirect without a session"
        endpoints = {r.endpoint for r in app.url_map.iter_rules()}
        for name in ("appointments.index", "appointments.book",
                     "appointments.cancel", "appointments.reschedule"):
            assert name in endpoints, f"10b2: {name} should be registered"

        # 10c. UX-03/UX-04 - the component layer reads tokens and nothing
        # else. a component that hardcodes a colour is the exact failure
        # UX-01 exists to prevent, it is invisible by eye across 500 lines,
        # and it is trivial to detect. comments are stripped first - they are
        # prose, and the token names quoted in them are not rules.
        components_src = (
            Path(__file__).resolve().parent
            / "shared" / "static" / "css" / "components.css"
        ).read_text()
        rules_only = re.sub(r"/\*.*?\*/", "", components_src, flags=re.S)
        assert not re.search(r"#[0-9a-fA-F]{3,8}\b", rules_only), \
            "10c: components.css must not hardcode a hex colour - use a --ds- token"
        assert not re.search(r"\brgba?\([^)]*\)", rules_only), \
            "10c: components.css must not hardcode an rgb/rgba colour - use a --ds- token"
        assert not re.search(r"font-size:\s*[^;]*\d+px", rules_only), \
            "10c: components.css must not set a raw px font-size - use a --ds- type token"
        assert "var(--ds-" in rules_only, \
            "10c: components.css must actually read the token layer"

        # 10d. UX-10 - the login's behaviour contract, not its looks.
        login_html = app.test_client().get("/login").text
        assert re.search(r"<button[^>]+data-pw-toggle[^>]+aria-pressed", login_html) or \
               re.search(r"<button[^>]+aria-pressed[^>]+data-pw-toggle", login_html), \
            "10d: show/hide must be a button carrying aria-pressed, not a checkbox or a div"
        assert login_html.count('name="password"') == 1, \
            "10d: the password must exist once in the dom - a second input would hold it in clear"
        # role comes from the authenticated identity and is never offered as
        # a choice. a select or a role field here would be an escalation path.
        assert "<select" not in login_html, "10d: no select on the login"
        assert 'name="role"' not in login_html, "10d: role must never be a submitted field"
        assert 'name="csrf_token"' in login_html, "10d: the csrf token must survive the restyle"
        assert 'action="/login"' in login_html or "url_for" not in login_html, \
            "10d: the form must still post to the login route"

        # 10e. UX-15 - one error voice across the staff surface. an error
        # rendered as a bare paragraph is both a different look and a weaker
        # signal: ds-alert carries role="alert" and is announced.
        staff_templates = (Path(__file__).resolve().parent / "app" / "templates")
        bare = [f.name for f in staff_templates.glob("*.html")
                if 'class="text-danger">{{ error' in f.read_text()]
        assert not bare, \
            f"10e: these still render an error as a bare paragraph, not an alert: {bare}"

        # 10f. UX-16 - both data tables carry the card treatment, and every
        # cell carries the label it shows on a phone. a data-label missing
        # from one column produces a card with an unlabelled value, which is
        # worse than a scrolling table.
        for tpl, table_file in (("patients_list.html", "patients_list.html"),
                                ("admin_users.html", "_user_row.html")):
            src = (staff_templates / tpl).read_text()
            assert "ds-table-cards" in src, f"10f: {tpl} table needs the card treatment"
            body = (staff_templates / table_file).read_text()
            tds = body.count("<td")
            labelled = body.count("data-label=")
            assert tds == labelled, \
                f"10f: {table_file} has {tds} cells but {labelled} data-labels"

        # 11. D-05 - login opts out of the sidebar; an authenticated screen
        # keeps it
        login_page = app.test_client().get("/login")
        def has_class(html, cls):
            return any(cls in attr.split()
                       for attr in re.findall(r'class="([^"]*)"', html))

        assert not has_class(login_page.text, "app-sidebar"), \
            "11: login must render without the sidebar"
        dash_with_shell = client_a.get("/")
        assert has_class(dash_with_shell.text, "app-sidebar"), \
            "11: an authenticated screen must render the sidebar"

        # ---- AUTH-05 staff password self-service ----

        def sign_in(username, password):
            client = app.test_client()
            csrf = _csrf_from(client.get("/login").text)
            resp = client.post(
                "/login",
                data={"username": username, "password": password, "csrf_token": csrf},
            )
            return client, resp

        def change_pw(client, current, new, confirm=None):
            page = client.get("/change-password")
            return client.post(
                "/change-password",
                data={
                    "current": current,
                    "password": new,
                    "confirm": new if confirm is None else confirm,
                    "csrf_token": _csrf_from(page.text),
                },
            )

        def db():
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            return conn

        def count_rows(sql, args=()):
            conn = db()
            n = conn.execute(sql, args).fetchone()[0]
            conn.close()
            return n

        def stored_hash(username):
            conn = db()
            row = conn.execute(
                "SELECT password_hash FROM users WHERE username = ?", (username,)
            ).fetchone()
            conn.close()
            return row["password_hash"]

        # 12. AUTH-05 SC1 + SC2 - a staff member changes their own password, the
        # new one works and the old one stops working
        _seed_user(db_path, "pchange", "oldpass123", "assistant")
        client_p, _ = sign_in("pchange", "oldpass123")
        assert client_p.get("/").status_code == 200, "12: sign-in should reach the dashboard"

        form = client_p.get("/change-password")
        assert form.status_code == 200, "12: change-password should be reachable"
        assert b'name="current"' in form.data, "12: the form must ask for the current password"
        assert str(MIN_PASSWORD_LENGTH).encode() in form.data, \
            "12: the form should state the real minimum length"

        changed = change_pw(client_p, "oldpass123", "brandnewpass1")
        assert changed.status_code == 302, "12: a successful change should redirect"
        assert changed.headers["Location"].endswith("/login"), \
            "12: a successful change should land on login (D-03)"

        _, old_login = sign_in("pchange", "oldpass123")
        assert old_login.status_code == 200, "12: the old password must stop working (SC2)"
        _, new_login = sign_in("pchange", "brandnewpass1")
        assert new_login.status_code == 302, "12: the new password must work immediately (SC2)"

        # 13. AUTH-05 SC4 - the completed change is audited
        assert count_rows(
            "SELECT COUNT(*) FROM audit_log WHERE action = 'change_password'"
            " AND allowed = 1 AND username = ?", ("pchange",)
        ) == 1, "13: a completed change should write one allowed change_password row"

        # 14. AUTH-05 SC3 - every session dies, not just the acting one. two
        # separate clients are the point: destroy_session would pass this with
        # only client A checked
        client_x, _ = sign_in("pchange", "brandnewpass1")
        client_y, _ = sign_in("pchange", "brandnewpass1")
        assert client_y.get("/").status_code == 200, "14: second session should start authenticated"

        change_pw(client_x, "brandnewpass1", "thirdpassword1")
        assert client_x.get("/").status_code == 302, "14: the acting session must be revoked"
        assert client_y.get("/").status_code == 302, \
            "14: every other session for that account must be revoked too (SC3)"
        assert count_rows(
            "SELECT COUNT(*) FROM sessions WHERE username = ?", ("pchange",)
        ) == 0, "14: no session rows should remain for the account"

        # 15. D-01/D-02 - a wrong current password refuses, writes nothing, and
        # is audited as change_password rather than login
        client_w, _ = sign_in("pchange", "thirdpassword1")
        hash_before = stored_hash("pchange")
        logins_before = count_rows("SELECT COUNT(*) FROM audit_log WHERE action = 'login'")

        refused = change_pw(client_w, "notmypassword", "yetanotherpass1")
        assert refused.status_code == 200, "15: a wrong current password should re-render, not redirect"
        assert b"Current password is not correct." in refused.data, "15: expected the refusal message"
        assert stored_hash("pchange") == hash_before, "15: nothing should have been written"
        assert client_w.get("/").status_code == 200, "15: the session should survive a refusal"
        assert count_rows(
            "SELECT COUNT(*) FROM audit_log WHERE action = 'change_password' AND allowed = 0"
        ) >= 1, "15: a failed check should be audited as change_password"
        assert count_rows("SELECT COUNT(*) FROM audit_log WHERE action = 'login'") == logins_before, \
            "15: a failed current-password check must not write a login row (D-02)"

        # 16. D-04 rules, and the forced first-change path still works
        hash_before = stored_hash("pchange")
        attempts_before = count_rows(
            "SELECT failed_attempts FROM users WHERE username = ?", ("pchange",)
        )

        short = change_pw(client_w, "thirdpassword1", "a" * (MIN_PASSWORD_LENGTH - 1))
        assert short.status_code == 200 and b"at least" in short.data, \
            "16: a too-short new password should be refused"
        assert stored_hash("pchange") == hash_before, "16: a refused change must write nothing"

        same = change_pw(client_w, "thirdpassword1", "thirdpassword1")
        assert same.status_code == 200 and b"different from your current one" in same.data, \
            "16: reusing the current password should be refused"
        assert stored_hash("pchange") == hash_before, "16: a refused change must write nothing"
        assert count_rows(
            "SELECT failed_attempts FROM users WHERE username = ?", ("pchange",)
        ) == attempts_before, \
            "16: rule violations must not burn a lockout attempt - the credential check runs last"

        conn = db()
        conn.execute(
            "INSERT INTO users (username, password_hash, role, active, must_change_password)"
            " VALUES (?, ?, ?, 1, 1)",
            ("forced", generate_password_hash("temp12345"), "dentist"),
        )
        conn.commit()
        conn.close()

        client_f, _ = sign_in("forced", "temp12345")
        forced_dash = client_f.get("/")
        assert forced_dash.status_code == 302 and "change-password" in forced_dash.headers["Location"], \
            "16: an account with must_change_password should be redirected to the form"
        forced_form = client_f.get("/change-password")
        assert b'name="current"' in forced_form.data, \
            "16: the forced path must also ask for the current password (D-01)"

        forced_done = change_pw(client_f, "temp12345", "forcednewpass1")
        assert forced_done.status_code == 302, "16: the forced change should succeed"
        assert count_rows(
            "SELECT must_change_password FROM users WHERE username = ?", ("forced",)
        ) == 0, "16: completing the change must clear must_change_password"

        # 18. GUI-12 / RBAC-03/04 - the dashboard's new KPI and chart data is
        # WITHHELD from a role that may not see it, not hidden from it. the
        # gate decides whether the query RUNS, so an assistant's response
        # carries no trace of the clinical figures. this is the D-09/D-10
        # pattern patients_routes.detail_view already uses for the clinical
        # card, applied to aggregates.
        #
        # NOTE on what is asserted where. the context assertions below are the
        # ones that BITE today: 29-03 owns dashboard.html and has not run yet,
        # so the template ignores these variables entirely and a body-only
        # assertion would pass whether the route withheld the data or handed
        # it over. the body assertions are kept alongside because they are the
        # ones that catch a template leaking the data once 29-03 renders it.
        # the two check different failure modes - the route computing what it
        # must not, and the template printing what it was not given.
        from flask import template_rendered

        _seed_user(db_path, "aneri", "goodpass", "assistant")

        seed_conn = sqlite3.connect(db_path)
        for cf, name in [
            ("KPIA800010150100", "Kpi Uno"),
            ("KPIB800010150100", "Kpi Due"),
            ("KPIC800010150100", "Kpi Tre"),
        ]:
            seed_conn.execute(
                "INSERT INTO patients (codice_fiscale, patient_name, phone)"
                " VALUES (?, ?, ?)", (cf, name, None),
            )
        # two distinct months so the series has a shape, and the month labels
        # are distinctive enough to grep an assistant's response body for
        for i, (cf, date) in enumerate([
            ("KPIA800010150100", "2031-03-04"),
            ("KPIB800010150100", "2031-03-19"),
            ("KPIC800010150100", "2031-07-22"),
        ]):
            seed_conn.execute(
                "INSERT INTO visits (codice_fiscale, visit_date, procedures,"
                " clinical_notes, next_appointment, source_path)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (cf, date, "[]", None, None, f"sorted/kpi_{i}.txt"),
            )
        seed_conn.commit()
        seed_conn.close()

        # one file per intake state that a per-user view can reach, filed
        # under each role so both dashboards have counts of their own
        audit_conn = sqlite3.connect(db_path)
        for who, role in [("drossi", "dentist"), ("aneri", "assistant")]:
            log_audit(audit_conn, who, role, "upload_file",
                      f"sorted/{who}_ok.txt", allowed=1)
            log_audit(audit_conn, who, role, "upload_file",
                      f"sorted/needs_review/{who}_bad.txt", allowed=1)
            log_audit(audit_conn, who, role, "upload_file",
                      f"{who}_nope.exe", allowed=0)
        audit_conn.close()

        def _dashboard_context(client):
            seen = []
            def _record(sender, template, context, **extra):
                seen.append(context)
            template_rendered.connect(_record, app)
            try:
                resp = client.get("/")
            finally:
                template_rendered.disconnect(_record, app)
            assert seen, "18: the dashboard should have rendered a template"
            return resp, seen[-1]

        client_dent = app.test_client()
        csrf_d = _csrf_from(client_dent.get("/login").text)
        client_dent.post("/login", data={
            "username": "drossi", "password": "goodpass", "csrf_token": csrf_d,
        })
        dent_resp, dent_ctx = _dashboard_context(client_dent)

        client_asst = app.test_client()
        csrf_s = _csrf_from(client_asst.get("/login").text)
        client_asst.post("/login", data={
            "username": "aneri", "password": "goodpass", "csrf_token": csrf_s,
        })
        asst_resp, asst_ctx = _dashboard_context(client_asst)

        # -- the dentist holds read_clinical, so the figures are computed
        assert dent_ctx["show_clinical"] is True, \
            "18: a dentist holds read_clinical and should see the clinical figures"
        assert dent_ctx["patient_total"] == 3, \
            f"18: dentist patient_total should be 3, got {dent_ctx['patient_total']}"
        assert dent_ctx["visit_months"] == ["2031-03", "2031-07"], \
            f"18: dentist visit months wrong: {dent_ctx['visit_months']}"
        assert dent_ctx["visit_counts"] == [2, 1], \
            f"18: dentist visit counts wrong: {dent_ctx['visit_counts']}"

        # -- the assistant does NOT hold read_clinical. the query never ran,
        # so the context carries None - not a zero, not an empty list, and
        # certainly not the real figure behind a template condition
        # the withholding assertions come FIRST, ahead of the show_clinical
        # flag. the flag is the weaker property: a route can report
        # show_clinical=False and still compute and hand over the figures for
        # a template to hide, which is precisely the bug D-10 forbids. put the
        # flag first and it shadows these under mutation.
        assert asst_ctx["patient_total"] is None, \
            "18: WITHHELD - an assistant's context must carry no patient total"
        assert asst_ctx["visit_months"] is None, \
            "18: WITHHELD - an assistant's context must carry no visit months"
        assert asst_ctx["visit_counts"] is None, \
            "18: WITHHELD - an assistant's context must carry no visit counts"
        assert asst_ctx["show_clinical"] is False, \
            "18: an assistant must not hold read_clinical"

        # -- and nothing of it reaches the wire either
        assert b"2031-03" not in asst_resp.data and b"2031-07" not in asst_resp.data, \
            "18: an assistant's response body must carry no visit-month label"

        # -- intake status is non-clinical (filenames and states), so both
        # roles that reach the dashboard get it, each scoped to their own files
        assert dent_ctx["show_intake"] is True and asst_ctx["show_intake"] is True, \
            "18: both dashboard-reaching roles hold upload_file"
        for who, ctx in [("dentist", dent_ctx), ("assistant", asst_ctx)]:
            counts = ctx["intake_counts"]
            assert counts["sorted"] == 1, f"18: {who} sorted count wrong: {counts}"
            assert counts["needs_review"] == 1, f"18: {who} needs_review count wrong: {counts}"
            assert counts["rejected"] == 1, f"18: {who} rejected count wrong: {counts}"

        # -- per-user scoping: the counts are the acting user's own files, so
        # the KPI row cannot contradict the intake list on the same screen
        assert sum(dent_ctx["intake_counts"].values()) == 3, \
            "18: intake counts must be per-user, not clinic-wide"

        # -- chart series arrive as plain parallel lists, shaped in python
        assert dent_ctx["intake_chart_labels"] == [
            "Sorted", "Needs Review", "Not searchable", "Queued", "External", "Rejected",
        ], "18: chart labels should mirror the badge labels in _recent_intake.html"
        assert dent_ctx["intake_chart_values"] == [1, 1, 0, 0, 0, 1], \
            f"18: chart values wrong: {dent_ctx['intake_chart_values']}"
        assert asst_ctx["intake_chart_labels"] is not None, \
            "18: an assistant may see intake status"

        # 18a. GUI-15 - a chart with nothing to draw renders copy, not an
        # empty canvas. chart.js draws no arcs for an all-zero doughnut and
        # reports nothing, so the card looked identical to a broken one
        # (30-AUDIT F-2). the verdict is computed in dashboard_routes, NOT in
        # jinja - the template still computes nothing (29 D-01).
        #
        # has-data and permitted are DIFFERENT questions and this section
        # exists to keep them from collapsing into one flag. three states:
        # not permitted -> no canvas and no empty state; permitted with no
        # data -> empty state; permitted with data -> canvas.
        assert dent_ctx["intake_has_data"] is True, \
            "18a: the dentist has three intake rows, so the doughnut has data"
        assert dent_ctx["visits_have_data"] is True, \
            "18a: the dentist has two visit months, so the bar chart has data"

        # the assistant is permitted intake and withheld clinical. the clinical
        # verdict must be False because the query never ran - not because the
        # figure happened to be empty.
        assert asst_ctx["intake_has_data"] is True, \
            "18a: an assistant's own intake rows still count"
        assert asst_ctx["visits_have_data"] is False, \
            "18a: WITHHELD - an assistant gets no visits verdict, the query never ran"

        # a permitted role with no rows of its own. seeded fresh with no audit
        # rows at all, so intake is genuinely zero - the reproduction case from
        # 31-BASELINE. still a dentist, so visits (clinic-wide) stay populated:
        # that is the point, the two verdicts are independent.
        _seed_user(db_path, "zerodent", "goodpass", "dentist")
        client_zero = app.test_client()
        csrf_z = _csrf_from(client_zero.get("/login").text)
        client_zero.post("/login", data={
            "username": "zerodent", "password": "goodpass", "csrf_token": csrf_z,
        })
        zero_resp, zero_ctx = _dashboard_context(client_zero)

        assert zero_ctx["show_intake"] is True, \
            "18a: a dentist holds upload_file - this is the permitted-but-empty case"
        assert zero_ctx["intake_chart_values"] == [0, 0, 0, 0, 0, 0], \
            f"18a: a fresh user's chart values should be all zero: {zero_ctx['intake_chart_values']}"
        assert zero_ctx["intake_has_data"] is False, \
            "18a: an all-zero series has no data to draw"
        assert zero_ctx["visits_have_data"] is True, \
            "18a: visits are clinic-wide - a fresh user still sees them, verdicts are independent"

        # the canvas must be ABSENT, not hidden. a canvas in the body with no
        # arcs on it is the defect; asserting only on the context would pass
        # against a template that emitted it anyway.
        assert b'id="intake-chart"' not in zero_resp.data, \
            "18a: an empty series must emit no canvas at all"
        assert b"Nothing filed yet" in zero_resp.data, \
            "18a: the empty intake card must say so"
        assert b'id="visits-chart"' in zero_resp.data, \
            "18a: the visits chart still has data and must still render"

        # and the withheld role gets NEITHER a canvas nor an empty state for
        # the figure it may not see - no trace, per D-10
        assert b'id="visits-chart"' not in asst_resp.data, \
            "18a: WITHHELD - an assistant gets no visits canvas"
        assert b"No dated visits yet" not in asst_resp.data, \
            "18a: WITHHELD - an assistant gets no visits empty state either"

        # 19. GUI-12 - the dashboard is the page most likely to break the
        # no-CDN rule, because it is the only one that loads a charting
        # library. section 10 already checks an authenticated page generally;
        # this pins the dashboard specifically, and pins WHERE chart.js comes
        # from rather than only that no external asset is present.
        dash_assets = client_dent.get("/")
        assert not re.search(
            r'<(?:script|link)[^>]+(?:src|href)="https?://', dash_assets.text, re.IGNORECASE
        ), "19: the dashboard must not reference any external http(s) asset"
        assert b"/static/vendor/chartjs/chart.umd.min.js" in dash_assets.data, \
            "19: chart.js must be served from /static, vendored offline"
        assert b"<canvas" in dash_assets.data, \
            "19: the dashboard should render at least one chart canvas"

        # the undo history is the one pre-existing feature on this page and a
        # restyle that drops it is a regression, not a redesign
        assert b"Recent changes" in dash_assets.data, \
            "19: the undo history must survive the dashboard restyle"

        # withheld, not hidden, at the MARKUP layer this time: an assistant
        # gets no canvas and no placeholder telling them a figure exists
        asst_assets = client_asst.get("/")
        assert b"visits-chart" not in asst_assets.data, \
            "19: an assistant must not receive the visits canvas"
        assert b"Visits per month" not in asst_assets.data, \
            "19: an assistant must not even see the visits card heading"
        assert b"intake-chart" in asst_assets.data, \
            "19: an assistant may see intake status"

        # 20. GUI-14 - the shell collapses below the lg breakpoint. the fast
        # suite has no browser, so it cannot measure scrollWidth; what it CAN
        # pin is the markup contract the collapse depends on. the measurement
        # itself lives in shot_pages.py.
        shell = client_dent.get("/")

        assert b"offcanvas-lg" in shell.data, \
            "20: the sidebar must be an offcanvas panel below the lg breakpoint"
        assert b'id="app-nav"' in shell.data, \
            "20: the offcanvas panel needs the id the toggle targets"
        assert b"app-topbar" in shell.data, \
            "20: an authenticated screen must carry the below-breakpoint top bar"

        # the toggle and the panel must actually refer to the same element. a
        # typo here leaves a hamburger that opens nothing while every other
        # assertion in this section still passes.
        #
        # scope this to the TOGGLE's own tag. a document-wide search for
        # data-bs-target matches the panel's close button first (_sidebar.html
        # is included above the top bar), which always names a real id - so the
        # naive version passed a deliberately broken hamburger. found by
        # mutation, not by review.
        toggle = re.search(rb'<button[^>]*data-bs-toggle="offcanvas"[^>]*>', shell.data)
        assert toggle, "20: the top bar must carry an offcanvas toggle button"
        tag = toggle.group(0)
        tgt = re.search(rb'data-bs-target="#([^"]+)"', tag)
        ctl = re.search(rb'aria-controls="([^"]+)"', tag)
        assert tgt, "20: the hamburger must declare a data-bs-target"
        assert ctl, "20: the hamburger must declare aria-controls"
        assert tgt.group(1) == ctl.group(1), \
            f"20: hamburger data-bs-target #{tgt.group(1).decode()} disagrees with " \
            f"aria-controls {ctl.group(1).decode()}"
        assert b'id="' + tgt.group(1) + b'"' in shell.data, \
            f"20: hamburger targets #{tgt.group(1).decode()}, which names no element on the page"

        assert b'aria-label="Open navigation"' in shell.data, \
            "20: the hamburger must be labelled for screen readers"
        assert b'aria-label="Close navigation"' in shell.data, \
            "20: the panel's close control must be labelled for screen readers"

        # the offcanvas wrapper must not have eaten a link, and the role filter
        # must still bite. counts are PER ROLE, not the 7 in the template
        # source: 'Staff accounts' is behind manage_users, so a dentist sees 6
        # and an admin sees a different 4. asserting the source count here
        # would pass even if authorize() had been stripped out entirely.
        # PROPERTY UNCHANGED: the role filter still bites, counted per role.
        # 6 -> 7 because phase 39 added Reports behind read_clinical, which a
        # dentist holds. 7 -> 8 because phase 41 added Appointments behind
        # manage_appointments, which a dentist also holds. The assertion below
        # that an assistant does NOT gain Reports is what keeps this honest -
        # a bare count going up could otherwise hide authorize() being
        # stripped out. Appointments is the opposite case and is asserted
        # separately: an assistant DOES gain it, and an admin must not.
        assert shell.data.count(b'class="nav-link') == 8, \
            f'20: a dentist should see 8 sidebar links, got {shell.data.count(chr(99).encode() + b"lass=\"nav-link")}'
        assert b"Reports" in shell.data, "20: a dentist holds read_clinical and is offered Reports"
        assert b"Staff accounts" not in shell.data, \
            "20: a dentist must not be offered the admin link"

        # 20a. UX-19 - Reports sits behind read_clinical, the dentist-only
        # capability. an assistant holds read_notes but NOT read_clinical, so
        # it must get neither the link nor the page - withheld, not hidden.
        client_asst = app.test_client()
        csrf_a2 = _csrf_from(client_asst.get("/login").text)
        _seed_user(db_path, "zasst2", "goodpass", "assistant")
        client_asst.post("/login", data={
            "username": "zasst2", "password": "goodpass", "csrf_token": csrf_a2,
        })
        asst_shell = client_asst.get("/")
        assert b"Reports" not in asst_shell.data, \
            "20a: an assistant must not be offered Reports"
        # APPT-05, the other direction: reception books, so an assistant DOES
        # hold manage_appointments and must be offered the link. asserting only
        # the absences would let the capability be dropped from assistant
        # without a single test noticing.
        assert b"Appointments" in asst_shell.data, \
            "20a: an assistant holds manage_appointments and is offered Appointments"
        assert client_asst.get("/appointments").status_code == 200, \
            "20a: and can reach the page"

        asst_reports = client_asst.get("/reports")
        assert asst_reports.status_code == 302, \
            "20a: an assistant must be refused the reports route, not shown a hidden page"
        assert b"chart" not in asst_reports.data.lower(), \
            "20a: and the refusal must carry no trace of the figures"

        client_adm = app.test_client()
        csrf_m = _csrf_from(client_adm.get("/login").text)
        _seed_user(db_path, "zadmin", "goodpass", "admin")
        client_adm.post("/login", data={
            "username": "zadmin", "password": "goodpass", "csrf_token": csrf_m,
        })
        adm_shell = client_adm.get("/admin/users")
        assert b"Staff accounts" in adm_shell.data, \
            "20: an admin must be offered the admin link"
        assert b"Appointments" not in adm_shell.data, \
            "20: an admin holds manage_users alone and must not be offered Appointments"

        # 20b. UX-20 - what the reports page must NEVER claim. invoices
        # carry no status and there is no payments table, and patients carry no
        # created date, so revenue and growth are unprovable and are excluded
        # outright rather than shown empty.
        #
        # "appointments over time" is a DIFFERENT case since phase 41: the
        # table exists, so the schema could support that chart. it stays
        # forbidden here only because nobody has built it, and the page says
        # exactly that. if a later phase charts it, this line should be
        # removed deliberately - not read as a schema limit that never lifted.
        rep = client_g.get("/reports")
        assert rep.status_code == 200, "20b: a dentist should reach the reports page"
        body = rep.data.lower()
        for forbidden in (b"revenue", b"turnover", b"profit",
                          b"appointments over time", b"new patients"):
            assert forbidden not in body, \
                f"20b: the reports page must not present {forbidden.decode()!r} - the schema cannot support it"
        # "collected" is allowed ONLY as a denial. the page says "billed, not
        # collected", which is the opposite of a claim - a flat ban on the
        # word would forbid the very sentence that makes the page honest, and
        # did, the first time this was written.
        flat = re.sub(r"\s+", " ", body.decode())
        for m in re.finditer(r"collected", flat):
            before = flat[max(0, m.start() - 12):m.start()]
            assert "not " in before, \
                f"20b: 'collected' may appear only as a denial, found: ...{before}collected..."
        assert b"billed" in body, \
            "20b: and it must say billed, which is what the invoice lines actually are"

        # 20c. every figure traces to a query. a sum computed in jinja is a
        # second source of truth for a number the route already owns (D-01).
        reports_tpl = (Path(__file__).resolve().parent / "app" / "templates" / "reports.html").read_text()
        for jinja_math in ("|sum", "|length", "sum(", "count("):
            assert jinja_math not in reports_tpl, \
                f"20c: reports.html computes {jinja_math!r} - figures belong in the route"

        # 20d. asserted per state, because the fixture db may hold no
        # procedures and a bare containment check would then be vacuous.
        # drawn: an uncoded term is NAMED, never distinguished by colour
        # alone (UX-17, and this is a clinical surface).
        # empty: phase 31's empty state, saying what will appear and why not.
        if b'id="proc-chart"' in rep.data:
            assert b"not in the procedure glossary" in rep.data, \
                "20d: an uncoded term must be named, not only tinted"
        else:
            assert b"No procedures yet" in rep.data, \
                "20d: an empty series must render the empty state, not a blank card"
        if b'id="billed-chart"' not in rep.data:
            assert b"Nothing billed yet" in rep.data, \
                "20d: an empty billed series must render the empty state"
        assert b"Patients" not in adm_shell.data.split(b"</aside>")[0], \
            "20: an admin holds no read_notes and must not be offered Patients"

        # login renders no sidebar, so it must render no hamburger either - a
        # toggle for a panel that was never included opens nothing
        login_shell = app.test_client().get("/login")
        assert b"app-topbar" not in login_shell.data, \
            "20: a chromeless page must not carry the top bar"
        assert b"offcanvas-lg" not in login_shell.data, \
            "20: a chromeless page must not carry the offcanvas panel"

        # 21. GUI-13/GUI-14 - the two rules phase 30 established, pinned so
        # they cannot silently regress. the horizontal-scroll measurement
        # itself needs a browser and lives in shot_pages.py; what belongs here
        # is the markup that measurement depends on.
        for path, who in (("/", client_dent), ("/patients", client_dent)):
            page = who.get(path)
            assert not re.search(rb'<[^>]+\sstyle="', page.data), \
                f"21: {path} must carry no inline style= attribute"

        adm_tbl = client_adm.get("/admin/users")
        pat_tbl = client_dent.get("/patients")
        assert b"table-responsive" in pat_tbl.data, \
            "21: the patients table must be wrapped so it scrolls inside its card"
        assert b"table-responsive" in adm_tbl.data, \
            "21: the staff-accounts table must be wrapped too"

        # a filename is user-supplied and unbounded. without min-w-0 the flex
        # child cannot shrink and one long name drags the page sideways -
        # measured at 1576px on a 390px viewport before this was fixed.
        intake = client_dent.get("/upload/recent")
        assert b"min-w-0" in intake.data, \
            "21: the intake filename cell must be allowed to shrink"
        assert b"text-break" in intake.data, \
            "21: the intake filename must be allowed to break"

        # 17. CR-05 - the staff app's session cookie must not be left at
        # Flask's default, and must differ from the patient app's own choice.
        # importing patient_app here is fine - the binding rule is about what
        # patient_app imports, and patient_app_selftest.py section 1 checks
        # that import graph directly.
        from patient_app import create_patient_app

        patient_test_app = create_patient_app(env_path=Path(tmp) / ".env.patient")
        assert app.config["SESSION_COOKIE_NAME"] != "session", \
            "17: the two apps share a host, and cookies are not port-scoped"
        assert app.config["SESSION_COOKIE_NAME"] != patient_test_app.config["SESSION_COOKIE_NAME"], \
            "17: the staff and patient apps must not share a cookie name"

    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    print("usage: python app_selftest.py --selftest")


if __name__ == "__main__":
    main()
