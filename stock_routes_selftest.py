"""Stock through the real routes (P08.04, T4): reception's scope, the dentist's,
the alert lifecycle on screen, and a schedule that is never shown as active."""
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

from werkzeug.security import generate_password_hash

import app.db as app_db
import inventory
import web_session
from app import create_app


def _client(app, db_path, username, role):
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT OR IGNORE INTO users (username, password_hash, role, active)"
                 " VALUES (?, ?, ?, 1)", (username, generate_password_hash("x"), role))
    conn.commit()
    token = web_session.create_session(conn, username, role)
    conn.close()
    client = app.test_client()
    client.set_cookie(web_session.COOKIE_NAME, token)
    return client


def _csrf(html):
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def _key(html):
    return re.search(r'name="key" value="([^"]+)"', html).group(1)


def selftest():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "clinic.sqlite")
        app_db.DB_PATH = db_path
        app_db.CHROMA_PATH = str(Path(tmp) / "chroma")
        app = create_app()
        app.config["TESTING"] = True
        dentist = _client(app, db_path, "st_dentist", "dentist")
        reception = _client(app, db_path, "st_assist", "assistant")

        # 1. the dentist adds an item; it starts empty, so it is already low
        page = dentist.get("/stock").text
        assert "daily schedule: not configured" in page and "last run never" in page, \
            "1: an unscheduled job is never shown as active"
        dentist.post("/stock/items", data={"name": "Carpule articaina", "unit": "pz",
                                           "threshold": "20", "csrf_token": _csrf(page)})
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        item_id = conn.execute("SELECT id FROM inventory_items").fetchone()[0]
        conn.close()
        assert "Low stock (1)" in dentist.get("/stock").text, "1: the new item shows as low"
        assert "1 item low on stock" in dentist.get("/").text, "1: and on the dashboard"

        # 2. reception receives (a double submit counts once), uses and counts;
        # it is not offered a correction, a threshold or a new item
        seen = reception.get(f"/stock/{item_id}").text
        assert "Correction" not in seen and "Alert threshold" not in seen, "2: dentist-only forms"
        assert "Add an item" not in reception.get("/stock").text, "2: no new items for reception"
        key = _key(seen)
        for _ in range(2):
            reception.post(f"/stock/{item_id}/move", data={
                "kind": "receipt", "quantity": "50", "key": key, "csrf_token": _csrf(seen)})
        reception.post(f"/stock/{item_id}/move", data={
            "kind": "consumption", "quantity": "8", "key": _key(seen) + "b", "csrf_token": _csrf(seen)})
        reception.post(f"/stock/{item_id}/move", data={
            "kind": "count", "quantity": "40", "reason": "weekly count", "key": _key(seen) + "c",
            "csrf_token": _csrf(seen)})
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        assert inventory.balance(conn, item_id) == 40, "2: 50 once, less 8, counted to 40"
        conn.close()
        for url, data in ((f"/stock/{item_id}/move", {"kind": "correction", "quantity": "-5",
                                                       "reason": "x", "key": "c-1"}),
                          (f"/stock/{item_id}/threshold", {"threshold": "1"}),
                          ("/stock/items", {"name": "Frese", "unit": "pz", "threshold": "3"})):
            reception.post(url, data=dict(data, csrf_token=_csrf(seen)))
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        assert inventory.balance(conn, item_id) == 40 and inventory.item(conn, item_id)["threshold"] == 20
        assert conn.execute("SELECT COUNT(*) FROM inventory_items").fetchone()[0] == 1, "2: no new item"

        # 3. the alert resolved on restock; an over-consumption is refused on screen
        assert not inventory.open_alerts(conn), "3: 40 > 20 resolved the alert"
        conn.close()
        resp = reception.post(f"/stock/{item_id}/move", data={
            "kind": "consumption", "quantity": "41", "key": "over-1", "csrf_token": _csrf(seen)},
            follow_redirects=True)
        assert "cannot go below zero" in resp.text, "3: the refusal is explained"

        # 4. use it down to the threshold: alert, mark as seen, still listed as low
        reception.post(f"/stock/{item_id}/move", data={
            "kind": "consumption", "quantity": "20", "key": "use-1", "csrf_token": _csrf(seen)})
        page = reception.get("/stock?show=low").text
        assert "Carpule articaina" in page and "Mark as seen" in page, "4: the low filter shows it"
        alert_id = re.search(r"/stock/alerts/(\d+)/acknowledge", page).group(1)
        reception.post(f"/stock/alerts/{alert_id}/acknowledge", data={"csrf_token": _csrf(page)})
        page = reception.get("/stock").text
        assert "seen by st_assist" in page and "Low stock (1)" in page, "4: seen is not resolved"

        # 5. history explains the balance, and admin sees none of it
        hist = " ".join(re.sub(r"<[^>]+>", " ", dentist.get(f"/stock/{item_id}").text).split())
        # P29 R-012: the history is a table - movement, then its signed change, then the balance it left
        for word in ("Received +50 50", "Used -8 42", "Counted (40 on the shelf) -2 40", "weekly count", "Used -20 20"):
            assert word in hist, f"5: history is missing {word!r}"
        admin = _client(app, db_path, "st_admin", "admin")
        assert admin.get("/stock").status_code == 302, "5: admin has no stock"
        assert "low on stock" not in admin.get("/", follow_redirects=True).text

        # 6. P29 U-009: a count or correction reason comes from a short list or Other; Other needs words
        seen = dentist.get(f"/stock/{item_id}").text
        assert 'name="reason_choice"' in seen and '<option value="other">Other' in seen, \
            "6 U-009: the stock reason has no list"
        for k, choice, typed in (("u9-1", "other", ""), ("u9-2", "Damaged", "box crushed")):
            dentist.post(f"/stock/{item_id}/move", data={
                "kind": "count", "quantity": "18", "reason_choice": choice, "reason": typed, "key": k,
                "csrf_token": _csrf(seen)})
        conn = sqlite3.connect(db_path)
        counts = [r[0] for r in conn.execute("SELECT reason FROM inventory_movements WHERE item_id = ?"
                                             " AND kind = 'count' ORDER BY id", (item_id,))]
        conn.close()
        assert counts == ["weekly count", "Damaged: box crushed"], f"6: other with no words, or the choice: {counts}"

        # 7. P29 R-012: the quantity and the threshold stand out, the history is a table whose balance column ends at
        # the balance on the page, and a refused movement keeps what was typed
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        now7, thr7 = inventory.balance(conn, item_id), inventory.item(conn, item_id)["threshold"]
        n7 = conn.execute("SELECT COUNT(*) FROM inventory_movements WHERE item_id = ?", (item_id,)).fetchone()[0]
        conn.close()
        page7 = dentist.get(f"/stock/{item_id}").text
        assert re.search(r'data-stock="balance">\s*' + str(now7) + r'\b', page7) and \
            re.search(r'data-stock="threshold">\s*' + str(thr7) + r'\b', page7), "7 R-012: quantity and threshold are not stated apart"
        hist7 = page7[page7.index('id="hist-title"'):]
        heads = re.findall(r'<th scope="col">([^<]+)</th>', hist7)
        assert heads == ["When", "Movement", "Change", "Balance after", "Who", "Reason"], f"7: history columns {heads}"
        after = [int(x) for x in re.findall(r'<td data-label="Balance after">(-?\d+)', hist7)]
        changes = [int(x) for x in re.findall(r'<td data-label="Change">([+-]\d+)', hist7)]
        assert len(after) == n7 and after[0] == now7, f"7: the balance column does not end at the balance: {after}"
        assert all(after[i] - changes[i] == after[i + 1] for i in range(len(after) - 1)), "7: a balance does not follow"
        refused7 = reception.post(f"/stock/{item_id}/move", data={
            "kind": "consumption", "quantity": "999", "reason": "kept words", "key": "r12-1",
            "csrf_token": _csrf(reception.get(f"/stock/{item_id}").text)}, follow_redirects=True).text
        assert "Not recorded" in refused7, "7: the refusal is not said"
        assert 'id="qty" name="quantity" type="number" required value="999"' in refused7 \
            and '<option value="consumption" selected>' in refused7 and ">kept words</textarea>" in refused7, \
            "7 R-012: the refused movement lost what was typed"
        assert 'value="999"' not in reception.get(f"/stock/{item_id}").text, "7: the kept values outlive the next page"

    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    print("usage: python stock_routes_selftest.py --selftest")
    sys.exit(1)


if __name__ == "__main__":
    main()
