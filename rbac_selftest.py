"""Every staff route, for every role, against auth.ROUTE_POLICY.

For each endpoint and role the request is made with a real session. A role
the policy denies must see nothing of the canary patient and change nothing;
a role it allows must get through; and the view must actually have asked for
the capability the table claims, so the table cannot drift from the code.
"""
import json
import sqlite3
import sys
import tempfile
import urllib.error
from datetime import timedelta
from pathlib import Path

from werkzeug.security import generate_password_hash

import app.db as app_db
import auth
import clinic_time
import patient_id
import pending_actions
import web_session
from app import (agent_routes, create_app, dashboard_routes, notes_routes, patients_routes,
                 qa_routes, upload_routes)

ROLES = ("dentist", "assistant", "admin")

CANARY_CF = "ZZRB800101010101"
CANARY_NAME = "Zelda Canary"
CANARY_PHONE = "3330001111"
CANARY_NOTE = "canary molar sealed"
OTHER_CF = "ZZRB800101010102"

# tables a refused request is allowed to touch: the refusal is audited, a
# session row records last_seen, and a refused confirm still burns the
# caller's own single-use token
BOOKKEEPING = {"audit_log", "sessions", "sqlite_sequence", "pending_actions"}


def _seed(db_path, root):
    conn = sqlite3.connect(db_path)
    # rb_target is the account the admin routes act on, so disabling it does
    # not sign out a role the matrix still has to test
    for username, role in [(f"rb_{r}", r) for r in ROLES] + [("rb_target", "assistant")]:
        conn.execute(
            "INSERT INTO users (username, password_hash, role, active) VALUES (?, ?, ?, 1)",
            (username, generate_password_hash("goodpass"), role),
        )
    pid = patient_id.seed_patient(conn, CANARY_CF, CANARY_NAME, CANARY_PHONE)
    patient_id.seed_patient(conn, OTHER_CF, "zelda canary", None)
    cur = conn.execute(
        "INSERT INTO visits (patient_id, visit_date, procedures, clinical_notes, source_path)"
        " VALUES (?, '2026-06-01', '[]', ?, 'rb1.json')",
        (pid, CANARY_NOTE),
    )
    visit_id = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO appointments (patient_id, dentist, starts_at, minutes, status,"
        " created_at, updated_at) VALUES (?, 'rb_dentist', '2030-01-08T09:00:00+00:00', 30,"
        " 'booked', '2026-09-22T08:00:00+00:00', '2026-09-22T08:00:00+00:00')",
        (pid,),
    )
    appt_id = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO data_requests (patient_id, kind, requested_by, requested_role,"
        " requested_at) VALUES (?, 'export', ?, 'patient', '2026-09-22T08:00:00+00:00')",
        (pid, pid))
    req_id = cur.lastrowid
    conn.commit()
    conn.close()
    notes = root / "sorted" / pid / "notes"
    notes.mkdir(parents=True)
    (notes / "rb1.json").write_text("{}")
    return {"cf": CANARY_CF, "visit_id": visit_id, "appointment_id": appt_id, "req_id": req_id,
            "username": "rb_target", "filename": "app.css"}


def _state(db_path, root):
    conn = sqlite3.connect(db_path)
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
    state = {}
    for table in tables:
        if table in BOOKKEEPING:
            continue
        state[table] = sorted(map(repr, conn.execute(f"SELECT * FROM {table}").fetchall()))
    conn.close()
    state["files"] = sorted(str(p.relative_to(root)) for p in root.rglob("*")
                            if p.is_file() and "clinic.sqlite" not in p.name
                            and "chroma" not in p.parts)
    return state


def _form(endpoint):
    if endpoint in ("patients.duplicates_merge", "patients.duplicates_dismiss"):
        return {"cf_a": CANARY_CF, "cf_b": OTHER_CF, "keep": CANARY_CF,
                "confirm": "yes", "reason": "not the same person"}
    if endpoint == "admin.apply":
        return {"action": "active", "active": "0"}
    if endpoint == "admin.create":
        return {"username": "rb_new", "role": "admin", "password": "temp-pass-1"}
    if endpoint == "notes.new_note":
        return {"raw_note": "canary note", "cf": CANARY_CF}
    if endpoint == "qa.qa_page":
        return {"question": "what did Zelda Canary have done", "cf": CANARY_CF}
    if endpoint in ("agent.command_page", "agent.edit_page"):
        return {"command": "set phone of Zelda Canary to 3336666666", "patient": CANARY_CF,
                "field": "phone", "value": "3336666666"}
    if endpoint == "patients.edit_submit":
        return {"field": "phone", "value": "3339999999"}
    if endpoint == "appointments.book":
        return {"cf": CANARY_CF, "dentist": "rb_dentist", "date": "2030-01-08",
                "time": "10:00", "minutes": "30"}
    return {}


def _prepare(endpoint, role, db_path, undo_log):
    # confirm and undo only ever act on the caller's own pending action or
    # undo entry, so each role is handed one of its own - otherwise the view
    # returns "expired" before any capability is asked and a denied role
    # passes without having been tested
    username = f"rb_{role}"
    if endpoint == "agent.confirm_change":
        payload = {"tool": "update_field", "args": {"field": "phone", "value": "3338888888"},
                   "cf": CANARY_CF, "diff_line": "phone", "before": CANARY_PHONE,
                   "target": "sqlite:patients.phone"}
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        token = pending_actions.create_pending_action(conn, username, role, payload)
        conn.close()
        return {"token": token}
    if endpoint == "agent.undo_change":
        entry = {"ts": "2026-09-22T08:00:00", "tool": "update_field", "username": username,
                 "codice_fiscale": CANARY_CF, "target": "sqlite:patients.phone",
                 "before": "3337777777"}
        with open(undo_log, "a") as f:
            f.write(json.dumps(entry) + "\n")
    return {}


class Spy:
    def __init__(self):
        self.calls = []
        self.real = auth.authorize

    def __call__(self, role, action):
        result = self.real(role, action)
        self.calls.append((role, action, result))
        return result


def _install(spy):
    # views import authorize by name, so each module's own reference is swapped
    for mod in list(sys.modules.values()):
        if mod is not None and getattr(mod, "authorize", None) is spy.real:
            setattr(mod, "authorize", spy)


def _uninstall(spy):
    for mod in list(sys.modules.values()):
        if mod is not None and getattr(mod, "authorize", None) is spy:
            setattr(mod, "authorize", spy.real)


def _methods(rule):
    return [m for m in ("GET", "POST") if m in rule.methods]


def _request(client, rule, method, args, extra=None):
    url = rule.rule
    for name in rule.arguments:
        url = url.replace(f"<int:{name}>", str(args[name]))
        url = url.replace(f"<path:{name}>", str(args[name]))
        url = url.replace(f"<{name}>", str(args[name]))
    if method == "GET":
        return method, client.get(url)
    data = _form(rule.endpoint)
    data.update(extra or {})
    return method, client.post(url, data=data)


def _no_model(*args, **kwargs):
    # what a stopped ollama looks like - every model call handles it
    raise urllib.error.URLError("model stubbed in rbac_selftest")


def _ordered(rules):
    # destructive allowed actions last, so a merge or a cancel cannot change
    # what the endpoints after it are tested against
    last = ("patients.duplicates_merge", "patients.duplicates_dismiss", "auth.logout")
    return sorted(rules, key=lambda r: (r.endpoint in last, r.endpoint))


def selftest():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        db_path = str(root / "clinic.sqlite")
        app_db.DB_PATH = db_path
        app_db.CHROMA_PATH = str(root / "chroma")
        sorted_root = root / "sorted"
        for mod in (agent_routes, notes_routes, patients_routes, upload_routes):
            mod.SORTED_ROOT = sorted_root
        upload_routes.DROP_DIR = root / "drop"
        agent_routes.UNDO_LOG = str(root / "undo_log.jsonl")
        for mod in (agent_routes, notes_routes, qa_routes):
            mod._urlopen = _no_model
        dashboard_routes.UNDO_LOG = str(root / "undo_log.jsonl")

        app = create_app()
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        args = _seed(db_path, root)

        rules = [r for r in app.url_map.iter_rules()]
        endpoints = {r.endpoint for r in rules}

        # 1. every endpoint has a policy, and the policy names no dead endpoint
        missing = sorted(endpoints - set(auth.ROUTE_POLICY))
        assert not missing, f"1: endpoints with no ROUTE_POLICY entry: {missing}"
        dead = sorted(set(auth.ROUTE_POLICY) - endpoints)
        assert not dead, f"1: ROUTE_POLICY names endpoints that do not exist: {dead}"
        for endpoint, need in auth.ROUTE_POLICY.items():
            known = need in ("public", "login") or any(
                need in caps for caps in auth.PERMISSIONS.values())
            assert known, f"1: {endpoint} needs {need!r}, which no role holds"

        # 2. without a session every non-public endpoint goes to the login page
        anon = app.test_client()
        for rule in _ordered(rules):
            if auth.ROUTE_POLICY[rule.endpoint] == "public":
                continue
            for m in _methods(rule):
                method, resp = _request(anon, rule, m, args)
                assert resp.status_code == 302 and "/login" in resp.headers["Location"], \
                    f"2: {method} {rule.rule} reachable without a session ({resp.status_code})"

        # 3. every endpoint, every role
        spy = Spy()
        _install(spy)
        checked = 0
        try:
            for rule in _ordered(rules):
                need = auth.ROUTE_POLICY[rule.endpoint]
                if need == "public":
                    continue
                # denied roles first: their refusal is proved against state the
                # allowed roles have not yet changed
                roles = sorted(ROLES, key=lambda r: need == "login" or spy.real(r, need))
                for role, m in [(r, m) for r in roles for m in _methods(rule)]:
                    allowed = need == "login" or spy.real(role, need)
                    conn = sqlite3.connect(db_path)
                    token = web_session.create_session(conn, f"rb_{role}", role)
                    conn.close()
                    client = app.test_client()
                    client.set_cookie(web_session.COOKIE_NAME, token)

                    extra = _prepare(rule.endpoint, role, db_path, str(root / "undo_log.jsonl"))
                    before = _state(db_path, root)
                    spy.calls.clear()
                    method, resp = _request(client, rule, m, args, extra)
                    where = f"{method} {rule.rule} as {role}"

                    if need != "login":
                        asked = [c for c in spy.calls if c[0] == role and c[1] == need]
                        assert asked, f"3: {where} [{resp.status_code} {resp.headers.get('Location')}] never checked {need!r} " \
                                      f"(saw {sorted({c[1] for c in spy.calls})})"

                    if allowed:
                        assert resp.status_code != 403, f"3: {where} refused but policy allows"
                        location = resp.headers.get("Location", "")
                        if rule.endpoint != "auth.logout":
                            assert "/login" not in location, f"3: {where} bounced to login"
                    else:
                        body = resp.get_data(as_text=True)
                        # the codice fiscale is not checked: the caller put it in
                        # the url, and a redirect echoes it back
                        for secret in (CANARY_NAME, CANARY_PHONE, CANARY_NOTE):
                            assert secret not in body, f"3: {where} leaked {secret!r}"
                        # a refusal can be a redirect, a bare 403, or a 200
                        # error fragment for htmx; what makes it a refusal is
                        # that the capability came back false
                        refused = [c for c in spy.calls if c[0] == role and c[1] == need
                                   and not c[2]]
                        assert refused, f"3: {where} not refused ({resp.status_code})"
                        after = _state(db_path, root)
                        changed = [k for k in before if before[k] != after.get(k)]
                        assert not changed, f"3: {where} was refused but changed {changed}"
                    checked += 1
        finally:
            _uninstall(spy)

        # 4. the recorded exception stays exactly as wide as it was decided
        admin_caps = auth.PERMISSIONS["admin"]
        assert admin_caps == {"manage_users"}, f"4: admin widened to {sorted(admin_caps)}"
        admin_patient = sorted(e for e, need in auth.ROUTE_POLICY.items()
                               if need == "manage_users" and e.startswith("patients."))
        assert admin_patient == ["patients.duplicates_dismiss", "patients.duplicates_merge",
                                 "patients.duplicates_view"], \
            f"4: admin reaches patient routes beyond duplicate review: {admin_patient}"

        # 5. the patient portal: every route outside its short public list
        # needs a patient session. the portal takes no patient id from the
        # url except an appointment id, and a foreign one is refused and
        # audited in patient_app_selftest 31d.
        from patient_app import create_patient_app, routes as patient_routes
        patient_routes.DB_PATH = db_path
        papp = create_patient_app(env_path=root / ".env.patient")
        papp.config["TESTING"] = True
        papp.config["WTF_CSRF_ENABLED"] = False
        public = {"static", "shared", "vendor", "patient.login", "set_language"}
        assert patient_routes.NO_SESSION_ALLOWED == public, \
            f"5: the portal's public list changed: {sorted(patient_routes.NO_SESSION_ALLOWED)}"
        anon = papp.test_client()
        portal = 0
        for rule in papp.url_map.iter_rules():
            if rule.endpoint in public:
                continue
            for m in _methods(rule):
                method, resp = _request(anon, rule, m, args)
                assert resp.status_code == 302 and "/login" in resp.headers["Location"], \
                    f"5: portal {method} {rule.rule} reachable without a session"
                portal += 1

        # 6. sessions and csrf (P06.T4). a bad file type, an oversized upload
        # and a locked login are covered in upload_routes_selftest 3 and 9 and
        # web_auth's selftest; these are the ones nothing else checked.
        forged = app.test_client()
        forged.set_cookie(web_session.COOKIE_NAME, "not-a-real-token")
        resp = forged.get("/reports")
        assert resp.status_code == 302 and "/login" in resp.headers["Location"], \
            "6a: a forged session cookie must go to the login page"

        conn = sqlite3.connect(db_path)
        stale = web_session.create_session(conn, "rb_dentist", "dentist",
                                           now=clinic_time.now_utc() - timedelta(hours=2))
        conn.close()
        idle = app.test_client()
        idle.set_cookie(web_session.COOKIE_NAME, stale)
        resp = idle.get("/reports")
        assert resp.status_code == 302 and "/login" in resp.headers["Location"], \
            "6b: an idle session must go to the login page"

        conn = sqlite3.connect(db_path)
        live = web_session.create_session(conn, "rb_dentist", "dentist")
        conn.close()
        demoted = app.test_client()
        demoted.set_cookie(web_session.COOKIE_NAME, live)
        assert demoted.get("/reports").status_code == 200, "6c: dentist should see reports"
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE users SET role = 'assistant' WHERE username = 'rb_dentist'")
        conn.commit()
        conn.close()
        resp = demoted.get("/reports")
        assert resp.status_code == 302 and "/login" not in resp.headers["Location"], \
            "6c: a demoted account must lose the old role on its very next request"
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE users SET role = 'dentist' WHERE username = 'rb_dentist'")
        conn.commit()
        conn.close()

        app.config["WTF_CSRF_ENABLED"] = True
        papp.config["WTF_CSRF_ENABLED"] = True
        csrf_checked = 0
        for role in ROLES:
            conn = sqlite3.connect(db_path)
            token = web_session.create_session(conn, f"rb_{role}", role)
            conn.close()
            client = app.test_client()
            client.set_cookie(web_session.COOKIE_NAME, token)
            for rule in rules:
                if "POST" not in rule.methods or rule.endpoint == "auth.login":
                    continue
                before = _state(db_path, root)
                method, resp = _request(client, rule, "POST", args)
                assert resp.status_code == 400, \
                    f"6d: POST {rule.rule} as {role} without a csrf token got {resp.status_code}"
                assert _state(db_path, root) == before, f"6d: POST {rule.rule} changed state"
                csrf_checked += 1
        pclient = papp.test_client()
        for rule in papp.url_map.iter_rules():
            if "POST" not in rule.methods:
                continue
            method, resp = _request(pclient, rule, "POST", args)
            assert resp.status_code == 400, \
                f"6d: portal POST {rule.rule} without a csrf token got {resp.status_code}"
            csrf_checked += 1

    print(f"selftest ok ({checked} role x route checks, {portal} portal routes,"
          f" {csrf_checked} csrf refusals)")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    print("usage: python rbac_selftest.py --selftest")
    sys.exit(1)


if __name__ == "__main__":
    main()
