"""P29 NEW-07 / E-008: the dental chart on the patient record. The dentist sees the chart and, on opening the record,
a popup with what the notes suggest; nothing suggested is drawn until approved there. Only the dentist reads or
changes it; every change is audited; the patient's copy holds it."""
import json
import re
import sys
import tempfile
from pathlib import Path

from werkzeug.security import generate_password_hash

import patient_id
import storage
import web_session

CF, CF_OTHER = "ZZDC800101010101", "ZZDC800101010102"


def selftest():
    with tempfile.TemporaryDirectory() as tmp:
        import app.db as app_db
        from app import create_app
        db = str(Path(tmp) / "chart.sqlite")
        app_db.DB_PATH = db
        app_db.CHROMA_PATH = str(Path(tmp) / "chroma")
        conn = storage.init_db(db)
        for user, role in (("dr_denti", "dentist"), ("desk", "assistant"), ("boss", "admin")):
            conn.execute("INSERT INTO users (username, password_hash, role, active) VALUES (?, ?, ?, 1)",
                         (user, generate_password_hash("x"), role))
        pid = patient_id.seed_patient(conn, CF, "Dora Chart", "+393400000011")
        other = patient_id.seed_patient(conn, CF_OTHER, "Otto Other", "+393400000012")
        for who, day, procs in ((pid, "2026-03-02", ["comp 36 MO", "prophy"]), (pid, "2026-04-10", ["ext 48"]),
                                (other, "2026-04-10", ["crown 21"])):
            conn.execute("INSERT INTO visits (patient_id, visit_date, procedures, clinical_notes, source_path)"
                         " VALUES (?, ?, ?, 'ok', ?)", (who, day, json.dumps(procs), f"sorted/{who}/{day}.json"))
        conn.commit()

        app = create_app()
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False

        def client(user, role):
            c = app.test_client()
            c.set_cookie(web_session.COOKIE_NAME, web_session.create_session(conn, user, role))
            return c
        dentist, desk, boss = client("dr_denti", "dentist"), client("desk", "assistant"), client("boss", "admin")

        def marks(status=None):
            q = "SELECT tooth, kind, status FROM tooth_marks WHERE patient_id = ?" + (" AND status = ?" if status else "")
            return sorted(tuple(r) for r in conn.execute(q, (pid, status) if status else (pid,)).fetchall())

        def drawn(page, kind):
            # a surface or a whole-tooth shape coloured as that kind, inside the chart itself
            part = page.split('data-dental-chart', 1)[1].split('data-chart-key', 1)[0]
            return re.findall(rf'<(?:polygon|rect|line|polyline) class="tc-[a-z]+ tc-k-{kind} tc-(?:done|planned)"(?: data-surface="(\w)")?', part)

        # 1. the dentist opens the record: the chart tab, and the popup with the two suggestions; nothing drawn yet
        page = dentist.get(f"/patients/{CF}").get_data(as_text=True)
        assert 'id="tabbtn-chart"' in page and 'id="chart"' in page, "1: no dental chart tab for the dentist"
        assert 'id="chartSuggest"' in page and "data-auto-open" in page, "1: the popup is not there"
        popup = page.split('id="chartSuggest"', 1)[1]
        assert popup.count("data-suggestion=") == 2 and "comp 36 MO" in popup and "ext 48" in popup, \
            "1: the popup does not hold the notes' suggestions"
        assert "crown 21" not in page, "1: HARD FAIL - another patient's note suggested here"
        assert drawn(page, "filling") == [] and drawn(page, "extraction") == [], "1: HARD FAIL - a suggestion drawn"
        for n in (18, 11, 21, 28, 48, 41, 31, 38, 55, 85):
            assert f'data-tooth="{n}"' in page, f"1: tooth {n} is not on the chart"
        assert marks() == [(36, "filling", "proposed"), (48, "extraction", "proposed")], f"1: {marks()}"
        dentist.get(f"/patients/{CF}")
        assert len(marks()) == 2, "1: opening again proposed twice"

        # 2. reception and admin see no chart, no popup, and cannot change it
        for who, name in ((desk, "reception"), (boss, "admin")):
            seen = who.get(f"/patients/{CF}").get_data(as_text=True)
            assert "tabbtn-chart" not in seen and "chartSuggest" not in seen and "data-dental-chart" not in seen, \
                f"2: {name} sees the dental chart"
        rows = conn.execute("SELECT id FROM tooth_marks WHERE patient_id = ? ORDER BY tooth", (pid,)).fetchall()
        fill_id, ext_id = rows[0][0], rows[1][0]
        for who in (desk, boss):
            who.post(f"/patients/{CF}/chart/decide", data={"all": "approve"})
            who.post(f"/patients/{CF}/chart/marks", data={"tooth": "11", "kind": "crown", "state": "done"})
        assert marks("confirmed") == [], "2: HARD FAIL - a non-dentist changed the chart"
        assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action LIKE 'dental_chart_%' AND allowed = 0")\
            .fetchone()[0] == 4, "2: the refusals are not audited"

        # 3. the popup's answer: approve one, refuse the other; the approved one is drawn on its surfaces
        resp = dentist.post(f"/patients/{CF}/chart/decide",
                            data={f"decision-{fill_id}": "approve", f"decision-{ext_id}": "reject"})
        assert resp.status_code == 302 and resp.headers["Location"].endswith("#chart"), "3: not back on the chart"
        assert marks() == [(36, "filling", "confirmed"), (48, "extraction", "rejected")], f"3: {marks()}"
        page = dentist.get(f"/patients/{CF}").get_data(as_text=True)
        assert sorted(drawn(page, "filling")) == ["M", "O"], f"3: the filling is not drawn on M and O: {drawn(page, 'filling')}"
        assert drawn(page, "extraction") == [] and 'id="chartSuggest"' not in page, "3: a refused suggestion stays"
        assert "approved by dr_denti" in page, "3: who approved it is not said"
        n = conn.execute("SELECT COUNT(*) FROM audit_log WHERE action IN ('dental_chart_approve', 'dental_chart_reject')"
                         " AND username = 'dr_denti'").fetchone()[0]
        assert n == 2, f"3: the decisions are not audited ({n})"

        # 4. the dentist's own marks: on the chart at once; a wrong one is refused with a reason
        dentist.post(f"/patients/{CF}/chart/marks", data={"tooth": "11", "kind": "crown", "state": "planned",
                                                          "surface": ["M", "O"], "marked_on": "2026-05-01"})
        dentist.post(f"/patients/{CF}/chart/marks", data={"tooth": "46", "kind": "caries", "state": "done",
                                                          "surface": ["D", "O", "V"]})
        bad = dentist.post(f"/patients/{CF}/chart/marks", data={"tooth": "19", "kind": "crown", "state": "done"},
                           follow_redirects=True).get_data(as_text=True)
        assert "That is not a tooth number." in bad, "4: an impossible tooth is not explained"
        assert marks("confirmed") == [(11, "crown", "confirmed"), (36, "filling", "confirmed"),
                                      (46, "caries", "confirmed")], f"4: {marks('confirmed')}"
        page = dentist.get(f"/patients/{CF}").get_data(as_text=True)
        assert drawn(page, "crown") == [""], f"4: a planned crown is not drawn as one ring: {drawn(page, 'crown')}"
        assert 'class="tc-ring tc-k-crown tc-planned"' in page, "4: the planned crown is not drawn as planned"
        assert sorted(drawn(page, "caries")) == ["B", "D", "O"], f"4: caries surfaces {drawn(page, 'caries')}"
        assert conn.execute("SELECT surfaces FROM tooth_marks WHERE tooth = 11").fetchone()[0] == "", \
            "4: a crown took surfaces"

        # 5. remove: off the chart, kept as removed; never through another patient's record
        crown_id = conn.execute("SELECT id FROM tooth_marks WHERE tooth = 11 AND patient_id = ?", (pid,)).fetchone()[0]
        assert dentist.post(f"/patients/{CF_OTHER}/chart/marks/{crown_id}/remove").status_code == 404, \
            "5: HARD FAIL - removed through another patient's record"
        dentist.post(f"/patients/{CF}/chart/marks/{crown_id}/remove")
        assert (11, "crown", "removed") in marks(), "5: a removed mark is not kept as removed"
        page = dentist.get(f"/patients/{CF}").get_data(as_text=True)
        assert drawn(page, "crown") == [], "5: a removed mark is still drawn"

        # 6. Approve all, on the other patient's record; and the page in Italian
        other_page = dentist.get(f"/patients/{CF_OTHER}").get_data(as_text=True)
        assert "crown 21" in other_page.split('id="chartSuggest"', 1)[1], "6: the other patient's suggestion"
        dentist.post(f"/patients/{CF_OTHER}/chart/decide", data={"all": "approve"})
        assert conn.execute("SELECT status FROM tooth_marks WHERE patient_id = ?", (other,)).fetchone()[0] == "confirmed"
        dentist.set_cookie("staff_lang", "it")
        it = dentist.get(f"/patients/{CF}").get_data(as_text=True)
        assert "Odontogramma" in it and "primo molare, inferiore destro" in it, "6: the chart is not in Italian"

        # 7. the patient's copy holds the chart as approved - never a suggestion or a removed mark
        import data_rights
        copy = data_rights.patient_data(conn, pid, sections=["visits"])
        assert sorted((m["tooth"], m["work"]) for m in copy["dental_chart"]) == [(36, "filling"), (46, "caries")], \
            f"7: {copy['dental_chart']}"
        assert "dental_chart" not in data_rights.patient_data(conn, pid, sections=["billing"]), "7: outside its section"
        conn.close()
    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        print("usage: python dental_chart_routes_selftest.py --selftest")
