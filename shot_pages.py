"""screenshot every staff page at one or more widths and measure horizontal overflow.

the staff app scrolls sideways below the sidebar breakpoint. phase 29 measured it
by hand twice with throwaway scripts; this is the same measurement, committed, so
the fix can be proven with one command instead of a third rewrite.

the check is document.documentElement.scrollWidth <= window.innerWidth + 1,
evaluated in a real browser. the +1 absorbs sub-pixel rounding.

exits 1 if any page-width overflows, or if the fixture cleanup leaves rows
behind. this is the only automated check on the no-horizontal-scroll rule -
app_selftest.py can see the markup that usually causes it, not the measurement.

    ollama serve                                       # not needed, no model here
    .venv/bin/python run.py                            # staff app, port 5000
    .venv/bin/python site_run.py                       # public site, port 5002
    .venv/bin/python -m playwright install chromium    # once

    .venv/bin/python shot_pages.py [--width 390] [--out DIR] [--headed]

seeds ZZS* users and one ZZS patient, deletes them in a finally. ZZS keeps these
rows clear of e2e_chat_walk.py's ZZE* and e2e_intake_walk.py's ZZI* namespaces so
none of the three can collide. deliberately NOT in run_selftests.sh - it needs a
server and a browser.

*** the screenshots contain patient names and codici fiscali. ***
they default to a temp directory, never the repo, so they cannot be committed by
accident. run this against fake fixtures only - it predates the real-data cutover
on purpose, and pointing it at real records writes patient data into png files.
"""

import sqlite3
import sys
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright
from werkzeug.security import generate_password_hash

STAFF_URL = "http://127.0.0.1:5000"
# the public site is a second target, not a second instrument. it must be
# running: UX-17 claims every page on all three apps, and a run that quietly
# skipped a third of the product would be the kind of green that is worse
# than no run at all.
SITE_URL = "http://127.0.0.1:5002"
DB_PATH = "db/clinic.sqlite"

PASS = "zzs_shotpass_1234"
USERS = (("zzs_dentist", "dentist"), ("zzs_assistant", "assistant"), ("zzs_admin", "admin"))

# ^[A-Z]{4}[0-9]{12}$ - ZZS* is this script's namespace
CF = "ZZSA850010150801"

# a page with no role is fetched logged-out. admin never reaches the dashboard:
# dashboard_routes.index redirects manage_users-without-read_notes to /admin/users.
PUBLIC_PAGES = [("login", "/login")]
# no session, no seeding - the site app holds no data to seed
SITE_PAGES = [
    ("home", "/"),
    ("services", "/services"),
    ("doctors", "/doctors"),
    ("clinic", "/clinic"),
    ("contact", "/contact"),
    ("reference", "/reference"),
]
ROLE_PAGES = {
    "dentist": [
        ("dashboard", "/"),
        ("patients", "/patients"),
        ("patient-detail", f"/patients/{CF}"),
        ("qa", "/qa"),
        ("reports", "/reports"),
        ("appointments", "/appointments"),
        ("notes-new", "/notes/new"),
        ("change-password", "/change-password"),
    ],
    "assistant": [
        ("dashboard", "/"),
        ("patients", "/patients"),
        ("patient-detail", f"/patients/{CF}"),
        # reception books, so the assistant holds manage_appointments too -
        # shot here as well because it is a different render from the
        # dentist's, not the same page behind the same gate
        ("appointments", "/appointments"),
    ],
    "admin": [
        ("admin-users", "/admin/users"),
    ],
}

MEASURE = """() => ({
  scroll: document.documentElement.scrollWidth,
  inner: window.innerWidth
})"""

RESULTS = []


# --- fixtures -------------------------------------------------------------


def seed():
    conn = sqlite3.connect(DB_PATH)
    for name, role in USERS:
        conn.execute(
            "INSERT INTO users (username, password_hash, role, active) VALUES (?, ?, ?, 1)",
            (name, generate_password_hash(PASS), role),
        )
    # the patients list needs at least one row or the table cannot overflow and
    # the measurement would be vacuous on the worst-affected page
    conn.execute(
        "INSERT INTO patients (codice_fiscale, patient_name, phone) VALUES (?, ?, ?)",
        (CF, "Zzs Shotpatient", "3330000000"),
    )
    conn.commit()
    conn.close()


def cleanup():
    conn = sqlite3.connect(DB_PATH)
    # child-first. no try/except: a delete that cannot run is a cleanup that did
    # not happen and it should be loud.
    for table in ("invoices", "visits", "patients"):
        conn.execute(f"DELETE FROM {table} WHERE codice_fiscale = ?", (CF,))
    for name, _ in USERS:
        conn.execute("DELETE FROM audit_log WHERE username = ?", (name,))
        conn.execute("DELETE FROM users WHERE username = ?", (name,))
    conn.commit()
    conn.execute("DELETE FROM audit_log WHERE target = ?", (CF,))
    conn.commit()
    users_left = conn.execute(
        "SELECT COUNT(*) FROM users WHERE username LIKE 'zzs%'"
    ).fetchone()[0]
    pats_left = conn.execute(
        "SELECT COUNT(*) FROM patients WHERE codice_fiscale LIKE 'ZZS%'"
    ).fetchone()[0]
    # this deleted from audit_log without ever counting what survived, so a
    # partial cleanup there was silent where a user or patient leak was loud.
    audit_left = conn.execute(
        "SELECT COUNT(*) FROM audit_log"
        " WHERE username LIKE 'zzs%' OR username LIKE 'ZZS%' OR target LIKE 'ZZS%'"
    ).fetchone()[0]
    conn.close()
    return users_left, pats_left, audit_left


# --- the walk -------------------------------------------------------------


def sign_in(page, username):
    page.goto(f"{STAFF_URL}/login")
    page.fill('input[name="username"]', username)
    page.fill('input[name="password"]', PASS)
    page.click('button[type="submit"]')
    page.wait_for_load_state("networkidle")
    if "/login" in page.url:
        raise RuntimeError(f"login failed for {username} - landed on {page.url}")


def shoot(page, width, role, name, path, out_dir, base=STAFF_URL):
    page.goto(f"{base}{path}")
    page.wait_for_load_state("networkidle")
    size = page.evaluate(MEASURE)

    shot_dir = out_dir / str(width)
    shot_dir.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(shot_dir / f"{role}-{name}.png"), full_page=True)

    over = size["scroll"] - size["inner"]
    ok = over <= 1
    RESULTS.append((width, role, path, size["scroll"], size["inner"], ok))
    verdict = "OK" if ok else f"OVERFLOW +{over}px"
    print(f"  {width:>5}  {role:<10} {path:<26} "
          f"scroll={size['scroll']:<5} inner={size['inner']:<5} {verdict}")


def walk(browser, width, out_dir):
    ctx = browser.new_context(viewport={"width": width, "height": 900})
    page = ctx.new_page()
    for name, path in PUBLIC_PAGES:
        shoot(page, width, "public", name, path, out_dir)
    ctx.close()

    ctx = browser.new_context(viewport={"width": width, "height": 900})
    page = ctx.new_page()
    for name, path in SITE_PAGES:
        shoot(page, width, "site", name, path, out_dir, base=SITE_URL)
    ctx.close()

    for username, role in USERS:
        ctx = browser.new_context(viewport={"width": width, "height": 900})
        page = ctx.new_page()
        sign_in(page, username)
        for name, path in ROLE_PAGES[role]:
            shoot(page, width, role, name, path, out_dir)
        ctx.close()


# --- entry point ----------------------------------------------------------


def parse_args(argv):
    widths = []
    out = None
    i = 0
    while i < len(argv):
        if argv[i] == "--width":
            widths.append(int(argv[i + 1]))
            i += 2
        elif argv[i] == "--out":
            out = Path(argv[i + 1])
            i += 2
        else:
            i += 1
    if not widths:
        widths = [1440, 390]
    if out is None:
        out = Path(tempfile.gettempdir()) / "dental-shots"
    return widths, out


def main():
    widths, out_dir = parse_args(sys.argv[1:])

    seed()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless="--headed" not in sys.argv)
            try:
                for width in widths:
                    print(f"\n--- {width}px")
                    walk(browser, width, out_dir)
            finally:
                browser.close()
    finally:
        users_left, pats_left, audit_left = cleanup()

    over = [r for r in RESULTS if not r[5]]
    print(f"\nshots: {out_dir}")
    print(f"fixtures left behind: {users_left} user(s), {pats_left} patient(s), "
          f"{audit_left} audit row(s)")
    if over:
        print(f"\n{len(over)} of {len(RESULTS)} page-widths scroll horizontally:")
        for width, role, path, scroll, inner, _ in over:
            print(f"  {width}px  {role:<10} {path:<26} {scroll} > {inner}")
    else:
        print(f"\nall {len(RESULTS)} page-widths fit - no horizontal scroll")

    # a failed cleanup and a horizontal scroll are both failures. the baseline
    # was red when this tool landed, so it exited 0 on overflow to stay
    # committable; that is no longer true and the gate 30-01 promised is here.
    if users_left or pats_left or audit_left:
        print("CLEANUP FAILED - ZZS rows survived")
        return 1
    if over:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
