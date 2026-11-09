"""Nana's card on the doctor's pages (owner, 2026-11-09), in a real browser - Chromium and WebKit (the Mac app's engine).

A throwaway clinic and a Nana service of its own (the real state machine and page server, no microphone: the walk
plays the interactions), each on a free port. The dentist's card starts off; turning it on hands the dentist's
session to the device (no password) and opens the microphone; what was asked is printed, then Nana thinks, then the
answer with an Open record button; the conversation survives opening the record; standby shrinks the card and an
interaction opens it again; Did I hear you right? is answered on the card; off closes the microphone and takes the
session back; a missing Nana says so; the card fits a phone; Italian. Screenshots go to the scratchpad given with
--shots (never the repository). usage: e2e_nana_card.py [--headed] [--shots DIR]
"""
import os
import socket
import sys
import tempfile
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright
from werkzeug.security import generate_password_hash
from werkzeug.serving import WSGIRequestHandler, make_server

import jarvis_link as jl
import patient_id
import storage
import web_session
from jarvis import states
from jarvis.web import create_app as service_app

CF = "ZZNA800101010101"
USER = "dr_card"


class Quiet(WSGIRequestHandler):
    def log(self, *a):
        pass


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main():
    headed = "--headed" in sys.argv
    shots = Path(sys.argv[sys.argv.index("--shots") + 1]) if "--shots" in sys.argv else None
    problems = []

    def check(ok, what):
        print(("ok   " if ok else "FAIL ") + what, flush=True)
        if not ok:
            problems.append(what)

    with tempfile.TemporaryDirectory() as tmp:
        staff_port, nana_port = free_port(), free_port()
        os.environ["NANA_URL"] = f"http://127.0.0.1:{nana_port}"
        import app.db as app_db
        from app import create_app
        db = str(Path(tmp) / "clinic.sqlite")
        app_db.DB_PATH = db
        app_db.CHROMA_PATH = str(Path(tmp) / "chroma")
        conn = storage.init_db(db)
        conn.execute("INSERT INTO users (username, password_hash, role, active) VALUES (?, ?, 'dentist', 1)",
                     (USER, generate_password_hash("x")))
        patient_id.seed_patient(conn, CF, "Marco Gallo", "+393400000077")
        conn.commit()
        device, _token = jl.register_device(conn, "Walk Mac", "admin_walk", "admin")
        m = states.Machine()
        m.go("READY", "listening")
        m.set_clinic(True, "connected as Walk Mac", device)
        servers = [make_server("127.0.0.1", staff_port, create_app(), request_handler=Quiet, threaded=True),
                   make_server("127.0.0.1", nana_port, service_app(m, clinic_url=f"http://127.0.0.1:{staff_port}",
                                                                   port=nana_port), request_handler=Quiet, threaded=True)]
        for s in servers:
            threading.Thread(target=s.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{staff_port}"

        def live():
            return conn.execute("SELECT username FROM jarvis_delegations WHERE device_id = ? AND revoked_at IS NULL",
                                (device,)).fetchall()

        def snap(page, name):
            if shots:
                shots.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(shots / f"{name}.png"))

        try:
            with sync_playwright() as p:
                for engine in (p.chromium, p.webkit):
                    tag = engine.name
                    m.set_paused(False)
                    m.set_access(None)
                    browser = engine.launch(headless=not headed)
                    ctx = browser.new_context(viewport={"width": 1280, "height": 860})
                    token = web_session.create_session(conn, USER, "dentist")
                    ctx.add_cookies([{"name": web_session.COOKIE_NAME, "value": token, "url": base}])
                    page = ctx.new_page()
                    page.goto(base + "/")
                    nana = page.locator("#nana")

                    # 1. off: the orb only; the card is closed
                    check(nana.get_attribute("data-mode") == "off" and page.locator("#nana-pill").is_visible()
                          and not page.locator("#nana-card").is_visible(), f"{tag} 1: starts off, the orb only")
                    snap(page, f"{tag}-1-off")

                    # 2. on: no password; the dentist's own session goes to the device; the microphone opens
                    before = conn.execute("SELECT COUNT(*) FROM jarvis_delegations").fetchone()[0]
                    page.click("#nana-pill")
                    page.wait_for_function("document.getElementById('nana').dataset.mode === 'on' && getComputedStyle(document.getElementById('nana-card')).opacity === '1'", timeout=8000)
                    page.wait_for_timeout(900)
                    check(page.locator("#nana-card").is_visible(), f"{tag} 2: the card opened")
                    check([tuple(r) for r in live()] == [(USER,)], f"{tag} 2: the session went to the device ({live()})")
                    check(not m.paused, f"{tag} 2: the microphone opened")
                    check(page.inner_text("#nana-state").strip().lower() == "ready" and "Hey Jarvis" in page.inner_text("#nana-why"),
                          f"{tag} 2: Ready, with the wake phrase in use")
                    m.set_access({"username": USER, "role": "dentist", "expires_at": "x", "kind": "web"})   # the link check
                    snap(page, f"{tag}-2-on")

                    # 3. what was asked is printed, then Nana thinks, then the answer - with Open record
                    t = m.begin("heard the wake phrase - listening to the request")
                    page.wait_for_function("document.getElementById('nana').dataset.phase === 'listening'", timeout=5000)
                    check("Listening" in page.inner_text("#nana-log"), f"{tag} 3: listening shown")
                    m.go_for(t, "ACTIVE", "working out what was asked (on this computer) - not recording")
                    m.heard_for(t, "Show me Marco's next appointment.")
                    page.wait_for_selector("#nana-log .nana-you", timeout=5000)
                    log = page.inner_text("#nana-log")
                    check("Show me Marco's next appointment." in log and "Thinking" in log, f"{tag} 3: printed, thinking")
                    snap(page, f"{tag}-3-thinking")
                    m.show_for(t, {"outcome": "patient_next", "title": "Next appointment", "private": True,
                                   "say": "The answer is on screen.", "heard": "Show me Marco's next appointment.",
                                   "choices": [{"cf": CF, "name": "Marco Gallo",
                                                "next": {"weekday": "Tuesday", "date": "17 Nov", "time": "10:30",
                                                         "dentist": USER, "minutes": 30}}],
                                   "citations": [], "warnings": []}, 60)
                    m.finish(t, "answered on screen")
                    page.wait_for_selector("#nana-log .nana-bot >> text=Open record", timeout=5000)
                    order = page.evaluate("""() => [...document.querySelectorAll('#nana-log .nana-msg')]
                                             .map((e) => e.classList.contains('nana-you') ? 'you' : 'nana')""")
                    check(order == ["you", "nana"] and "Thinking" not in page.inner_text("#nana-log"),
                          f"{tag} 3: the question, then the answer ({order})")
                    check("Tuesday 17 Nov, 10:30" in page.inner_text("#nana-log"), f"{tag} 3: the appointment")
                    page.wait_for_timeout(500)
                    olds = page.evaluate("() => document.querySelectorAll('#nana-log .nana-msg.nana-new').length")
                    m.set_paused(True)              # the conversation is drawn again on each change of what Nana does
                    page.wait_for_function("document.getElementById('nana').dataset.phase === 'paused'", timeout=5000)
                    m.set_paused(False)
                    page.wait_for_function("document.getElementById('nana').dataset.phase === 'ready'", timeout=5000)
                    page.wait_for_timeout(120)
                    again = page.evaluate("() => [...document.querySelectorAll('#nana-log .nana-msg')].map((e) => getComputedStyle(e).opacity)")
                    check(all(o == "1" for o in again), f"{tag} 3: an update does not fade the conversation again ({again}, {olds} new)")
                    heights = page.evaluate("""() => [...document.querySelectorAll('#nana .btn')].filter((b) => b.offsetParent)
                                               .map((b) => Math.round(b.getBoundingClientRect().height))""")
                    check(len(set(heights)) == 1, f"{tag} 3: one button size on the card ({heights})")
                    snap(page, f"{tag}-3-answer")

                    # 4. Open record opens it; the card and the conversation come along, no second hand-over
                    page.click("#nana-log >> text=Open record")
                    page.wait_for_url(f"**/patients/{CF}", timeout=8000)
                    page.wait_for_function("document.getElementById('nana').dataset.mode === 'on' && getComputedStyle(document.getElementById('nana-card')).opacity === '1'", timeout=5000)
                    check("Show me Marco's next appointment." in page.inner_text("#nana-log"), f"{tag} 4: the conversation kept")
                    check(len(live()) == 1, f"{tag} 4: one hand-over, not one per page ({len(live())})")
                    n = conn.execute("SELECT COUNT(*) FROM jarvis_delegations").fetchone()[0] - before
                    check(n == 1, f"{tag} 4: no new delegation on the next page ({n})")

                    # 5. standby: the orb, still listening; Nana spoken to opens the card by itself
                    page.click('#nana [data-nana="standby"]')
                    page.wait_for_timeout(700)
                    check(nana.get_attribute("data-mode") == "standby" and not page.locator("#nana-card").is_visible(),
                          f"{tag} 5: standby shrinks the card")
                    check(not m.paused, f"{tag} 5: standby keeps listening")
                    snap(page, f"{tag}-5-standby")
                    t = m.begin("heard the wake phrase - listening to the request")
                    page.wait_for_function("document.getElementById('nana').dataset.mode === 'on' && getComputedStyle(document.getElementById('nana-card')).opacity === '1'", timeout=5000)
                    check(True, f"{tag} 5: spoken to, the card opens")

                    # 6. Did I hear you right? answered on the card
                    m.go_for(t, "ACTIVE", "working out what was asked (on this computer) - not recording")
                    m.heard_for(t, "what is error E05")
                    pid = m.offer(t, "what is error E05", 30)
                    page.wait_for_selector("#nana-fix", timeout=5000)
                    page.fill("#nana-fix", "what is error E05 on the AX-200")
                    snap(page, f"{tag}-6-confirm")
                    page.click("#nana-log >> text=Yes, ask this")
                    got = m.await_decision(pid, within=5)
                    check(got == "ask" and m.corrected() == "what is error E05 on the AX-200",
                          f"{tag} 6: the corrected question was confirmed ({got})")
                    m.show_for(t, {"outcome": "answer", "heard": "what is error E05", "corrected": "what is error E05 on the AX-200",
                                   "citations": [{"passage": "E05: the door is open. Close it and press start.",
                                                  "title": "AX-200 manual", "page": 12, "source_id": 3, "version": 1}],
                                   "warnings": []}, 60)
                    m.finish(t, "answered on screen")
                    page.wait_for_selector("#nana-log >> text=Open page 12", timeout=5000)
                    check("E05: the door is open" in page.inner_text("#nana-log"), f"{tag} 6: the guide's answer")
                    snap(page, f"{tag}-6-answer")

                    # 7. off: the microphone closes, the session is taken back, the conversation goes
                    page.click('#nana [data-nana="power"]')
                    page.wait_for_function("document.getElementById('nana').dataset.mode === 'off'", timeout=5000)
                    page.wait_for_timeout(1200)
                    check(m.paused and live() == [], f"{tag} 7: off closes the microphone and ends the hand-over")
                    kept = page.evaluate("() => { try { return sessionStorage.getItem('nana-log:%s'); } catch (e) { return null; } }" % USER)
                    check(kept is None, f"{tag} 7: the conversation is gone")
                    snap(page, f"{tag}-7-off")

                    # 8. a reload stays off; Nana not running is said
                    page.reload()
                    check(nana.get_attribute("data-mode") == "off", f"{tag} 8: off stays off")
                    page.route(f"http://127.0.0.1:{nana_port}/**", lambda route: route.abort())
                    page.click("#nana-pill")
                    page.wait_for_selector("#nana-note:not([hidden])", timeout=8000)
                    check("not running" in page.inner_text("#nana-note") and nana.get_attribute("data-mode") == "off",
                          f"{tag} 8: Nana not running is said")
                    page.unroute(f"http://127.0.0.1:{nana_port}/**")
                    snap(page, f"{tag}-8-not-running")

                    # 9. a phone: the card fits
                    page.set_viewport_size({"width": 390, "height": 760})
                    page.click("#nana-pill")
                    page.wait_for_function("document.getElementById('nana').dataset.mode === 'on' && getComputedStyle(document.getElementById('nana-card')).opacity === '1'", timeout=8000)
                    page.wait_for_timeout(900)
                    box = page.locator("#nana-card").bounding_box()
                    check(box and box["x"] >= 0 and box["x"] + box["width"] <= 390 and box["y"] >= 0,
                          f"{tag} 9: the card fits 390 px ({box})")
                    snap(page, f"{tag}-9-phone")
                    page.click('#nana [data-nana="power"]')
                    page.wait_for_function("document.getElementById('nana').dataset.mode === 'off'", timeout=5000)
                    page.wait_for_timeout(600)

                    # 10. Italian
                    ctx.add_cookies([{"name": "staff_lang", "value": "it", "url": base}])
                    page.goto(base + "/")
                    check(page.get_attribute("#nana-pill", "aria-label") == "Accendi Nana", f"{tag} 10: Italian")
                    ctx.close()
                    browser.close()
        finally:
            for s in servers:
                s.shutdown()
            conn.close()
    print(f"{len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
