"""P29 NEW-07 / E-008: the dental chart in a real browser. A dentist opens a record whose notes name work on teeth:
the popup opens by itself with the suggestions and nothing is drawn yet; approving one draws it, refusing the other
leaves it off; clicking a tooth chooses it in the form and shows only its marks; a planned crown added by hand is
drawn as planned. The page never scrolls sideways (the chart scrolls inside its box on a phone). Chromium and WebKit
(the macOS app's engine), at desk and phone width, on a throwaway clinic of its own.
usage: e2e_dental_chart.py [--headed] [--shots DIR]"""
import json
import sys
import tempfile
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright
from werkzeug.security import generate_password_hash
from werkzeug.serving import WSGIRequestHandler, make_server

import patient_id
import storage
import web_session

CF = "ZZDW800101010101"


class Quiet(WSGIRequestHandler):
    def log(self, *a, **k):
        pass


def serve(app):
    server = make_server("127.0.0.1", 0, app, request_handler=Quiet, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def seed(conn):
    pid = patient_id.seed_patient(conn, CF, "Walter Chart", "+393400000021")
    for day, procs in (("2026-03-02", ["comp 36 MO", "prophy"]), ("2026-04-10", ["ext 48"])):
        conn.execute("INSERT INTO visits (patient_id, visit_date, procedures, clinical_notes, source_path)"
                     " VALUES (?, ?, ?, 'ok', ?)", (pid, day, json.dumps(procs), f"sorted/{pid}/{day}.json"))
    conn.commit()
    return pid


def walk(p, engine, app, conn, headed, shots):
    server = serve(app)
    base = f"http://127.0.0.1:{server.server_port}"
    browser = getattr(p, engine).launch(headless=not headed)
    try:
        for width in (1280, 390):
            conn.execute("DELETE FROM tooth_marks")  # each width starts from the notes alone
            conn.commit()
            token = web_session.create_session(conn, "dw_dentist", "dentist")
            ctx = browser.new_context(viewport={"width": width, "height": 900})
            ctx.add_cookies([{"name": web_session.COOKIE_NAME, "value": token, "url": base}])
            page = ctx.new_page()
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            where = f"{engine} {width}"

            # the popup opens by itself with both suggestions; nothing is drawn behind it
            page.goto(f"{base}/patients/{CF}")
            popup = page.locator("#chartSuggest")
            popup.wait_for(state="visible", timeout=10000)
            assert popup.locator("[data-suggestion]").count() == 2, f"{where}: the popup does not hold two suggestions"
            assert page.locator(".tc-svg .tc-k-filling").count() == 0, f"{where}: HARD FAIL - a suggestion drawn"
            if shots:
                page.wait_for_timeout(600)  # past the popup's fade-in, so the picture shows it as a person sees it
                page.screenshot(path=str(Path(shots) / f"chart-popup-{engine}-{width}.png"))

            # approve the filling, refuse the extraction
            rows = popup.locator("[data-suggestion]")
            for i in range(rows.count()):
                row = rows.nth(i)
                choice = "Approve" if "comp 36" in row.inner_text() else "Refuse"
                row.locator("label", has_text=choice).click()
            with page.expect_navigation(timeout=15000):
                popup.locator("button[type=submit]", has_text="Save my choices").click()
            page.locator("#chart").wait_for(state="visible", timeout=10000)
            assert page.locator("#chartSuggest").count() == 0, f"{where}: the popup came back after deciding"
            assert page.locator('#tab-chart').is_visible(), f"{where}: the redirect did not open the chart tab"
            t36 = page.locator('.tc-tooth[data-tooth="36"]')
            assert t36.locator(".tc-s.tc-k-filling").count() == 2, f"{where}: the filling is not drawn on two surfaces"
            assert page.locator('.tc-tooth[data-tooth="48"] .tc-cross').count() == 0, f"{where}: the refused one drawn"

            # clicking a tooth chooses it in the form and shows only its marks
            page.locator('.tc-tooth[data-tooth="11"]').click()
            assert page.input_value("#chart-tooth") == "11", f"{where}: the click did not choose tooth 11"
            assert page.locator('.tc-tooth[data-tooth="11"]').get_attribute("aria-pressed") == "true"
            assert page.locator("#chart-marks > tr:not([hidden])").count() == 0, f"{where}: other teeth's marks shown"
            page.locator("[data-chart-filter-clear]").click()
            assert page.locator("#chart-marks > tr:not([hidden])").count() == 1, f"{where}: Show every tooth"

            # a planned crown by hand; the surfaces are offered only for work that sits on them
            page.locator('.tc-tooth[data-tooth="11"]').click()
            page.locator('label[for="chart-kind-crown"]').click()
            assert page.eval_on_selector("#chart-surfaces", "e => e.disabled"), f"{where}: surfaces offered for a crown"
            page.locator('label[for="chart-kind-filling"]').click()
            assert not page.eval_on_selector("#chart-surfaces", "e => e.disabled"), f"{where}: surfaces not offered for a filling"
            page.locator('label[for="chart-kind-crown"]').click()
            page.locator("#chart-state-planned").check()
            with page.expect_navigation(timeout=15000):
                page.locator("#chart-form button[type=submit]").click()
            assert page.locator('.tc-tooth[data-tooth="11"] .tc-ring.tc-k-crown.tc-planned').count() == 1, \
                f"{where}: the planned crown is not drawn as planned"

            # never a sideways page; on a phone the chart scrolls in its own box
            wide = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
            assert wide <= 0, f"{where}: the page scrolls sideways by {wide}px"
            if width == 390:
                inner = page.evaluate("(() => { const s = document.querySelector('.tc-scroll');"
                                      " return s.scrollWidth > s.clientWidth; })()")
                assert inner, f"{where}: the chart does not scroll inside its box"
            if shots:
                page.locator("#chart").screenshot(path=str(Path(shots) / f"chart-{engine}-{width}.png"))
                page.screenshot(path=str(Path(shots) / f"chart-page-{engine}-{width}.png"), full_page=True)
            assert not errors, f"{where}: script errors {errors}"
            ctx.close()
            print(f"PASS {where}: popup by itself, nothing drawn before approval, approve/refuse, tooth choice, "
                  f"planned crown, no sideways page")
    finally:
        browser.close()
        server.shutdown()
        server.server_close()


def main():
    headed = "--headed" in sys.argv
    shots = sys.argv[sys.argv.index("--shots") + 1] if "--shots" in sys.argv else None
    with tempfile.TemporaryDirectory() as tmp:
        import app.db as app_db
        from app import create_app
        db = str(Path(tmp) / "chart.sqlite")
        app_db.DB_PATH = db
        app_db.CHROMA_PATH = str(Path(tmp) / "chroma")
        conn = storage.init_db(db)
        conn.execute("INSERT INTO users (username, password_hash, role, active) VALUES ('dw_dentist', ?, 'dentist', 1)",
                     (generate_password_hash("x"),))
        conn.commit()
        seed(conn)
        app = create_app()
        with sync_playwright() as p:
            for engine in ("chromium", "webkit"):
                walk(p, engine, app, conn, headed, shots)
        conn.close()


if __name__ == "__main__":
    main()
