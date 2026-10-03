"""The app reads the synthetic Drive test folder itself (DRV, P25-D4): read-only sync -> stage -> review.

No network: a fake Drive API v3 answers in-process, and once over real HTTP on 127.0.0.1 so the urllib path,
status mapping and key handling are exercised. Expectations frozen in .planning/plans/DRV.md before the code.
"""
import json
import sqlite3
import sys
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import app.db as app_db
import clinic_time
import documents as docs
import drive_source as ds
import legacy_fixtures as fx
import legacy_import as li
import patient_id
from patient_files_selftest import csrf, staff_client

D, A, ADM = ("drossi", "dentist"), ("aassist", "assistant"), ("anadmin", "admin")
FOLDER = "fakefolder0000000000000000000001"
KEY = "AIza" + "x" * 35                      # the shape of a key, obviously not one
T0 = clinic_time.read_instant("2026-10-03T08:00:00+00:00")
D06 = (b"Referto DEMO (dati inventati)\nPaziente: Marco Gallo\nCodice fiscale: " + fx.DRIVE_PEOPLE["marco"][0].encode()
       + b"\n")


class FakeDrive:
    """Drive API v3 as far as the app may use it: files.list on one folder, files.get?alt=media."""

    def __init__(self):
        self.files = {}
        self.calls = []
        self.deny = None          # 403 / 404 on every call
        self.crash_after = None   # raise after this many downloads
        self.downloads = 0
        self.corrupt = set()      # file ids served with bytes that do not match their md5
        self.drop_after = None    # the network drops after this many downloads

    def add(self, fid, name, data, mime="application/octet-stream"):
        import hashlib
        self.files[fid] = {"id": fid, "name": name, "mimeType": mime, "data": data, "size": str(len(data)),
                           "md5Checksum": hashlib.md5(data).hexdigest(), "modifiedTime": "2026-10-03T08:00:00.000Z"}

    def __call__(self, method, url, timeout):
        self.calls.append((method, url))
        u = urllib.parse.urlparse(url)
        q = urllib.parse.parse_qs(u.query)
        if self.deny == 400:   # what Drive really answers for a deleted or invalid key
            return 400, json.dumps({"error": {"code": 400, "message": "API key not valid. Please pass a valid API key.",
                                              "status": "INVALID_ARGUMENT",
                                              "details": [{"reason": "API_KEY_INVALID"}]}}).encode()
        if self.deny:
            return self.deny, json.dumps({"error": {"code": self.deny, "message": "denied",
                                                    "status": "PERMISSION_DENIED"}}).encode()
        if u.path.endswith("/files") and method == "GET":
            assert f"'{FOLDER}' in parents" in q["q"][0], "only the configured folder is listed"
            meta = [{k: v for k, v in f.items() if k != "data"} for f in self.files.values()]
            if q.get("pageToken"):
                return 200, json.dumps({"files": meta[2:]}).encode()
            return 200, json.dumps({"files": meta[:2], "nextPageToken": "p2"} if len(meta) > 2
                                   else {"files": meta}).encode()
        fid = u.path.rsplit("/", 1)[-1]
        if q.get("alt") == ["media"] and fid in self.files:
            if self.crash_after is not None and self.downloads >= self.crash_after:
                raise KeyboardInterrupt("power cut")
            if self.drop_after is not None and self.downloads >= self.drop_after:
                raise ds.DriveError("network", "Drive could not be reached (ConnectionResetError)")
            self.downloads += 1
            data = self.files[fid]["data"]
            return 200, (data + b"x" if fid in self.corrupt else data)
        return 404, b'{"error": {"code": 404, "message": "not found"}}'


def counts(conn):
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("import_batches", "import_items", "patient_documents", "document_publications", "drive_files")}


def setup_drive():
    with tempfile.TemporaryDirectory() as built:
        cases = fx.build_drive(Path(built))
        fake = FakeDrive()
        for i, p in enumerate(sorted(Path(built).iterdir())):
            if p.is_file():
                fake.add(f"id{i:02d}", p.name, p.read_bytes())
    return fake, cases


def service(tmp, conn, pids):
    # 1. nothing configured: not connected, says why, reads nothing
    ds.FOLDER_ID, ds.KEY_FILE = "", tmp / "no-key"
    st = ds.status(conn)
    assert not st["configured"] and "folder" in st["reason"], st
    try:
        ds.sync(conn, *D, now=T0)
        raise AssertionError("1: synced with nothing configured")
    except ds.DriveError as e:
        assert e.code == "not_configured", e.code
    ds.FOLDER_ID = FOLDER
    st = ds.status(conn)
    assert not st["configured"] and "key" in st["reason"], "1: no key is reported, without the key"
    (tmp / "key").write_text(KEY + "\n")
    ds.KEY_FILE = tmp / "key"
    assert ds.status(conn)["configured"], "1: folder and key -> connected"

    fake, cases = setup_drive()
    ds.TRANSPORT = fake
    before = counts(conn)
    # 2. first sync: every file listed (two pages), downloaded once, md5-checked, staged; nothing attached
    run = ds.sync(conn, *D, now=T0)
    r = ds.run(conn, run)
    assert r["status"] == "ok" and r["listed"] == 6 and r["new_files"] == 6, dict(r)
    mirror = ds.mirror_dir()
    assert sorted(p.name for p in mirror.iterdir()) == sorted(f["name"] for f in fake.files.values()), \
        "2: the app's own mirror holds exactly the listed files"
    items = {Path(i["rel_path"]).name: i for i in li.items(conn, None)}
    expect = {"D01-referto-bruno.pdf": ("proposed", "strong"), "D02-referto-due-codici.txt": ("conflict", None),
              "D03-lettera-santoro.txt": ("unmatched", None), "D04-scan-senza-dati.jpg": ("unmatched", None),
              "D05-referto-bruno-copia.pdf": ("proposed", "strong")}
    for name, (state, strength) in expect.items():
        if name not in items and name.endswith(".jpg"):
            continue  # no sips on this machine: D04 is not built
        assert (items[name]["state"], items[name]["strength"]) == (state, strength), \
            f"2: {name} expected {state}/{strength}, got {items[name]['state']}/{items[name]['strength']}"
    assert counts(conn)["patient_documents"] == before["patient_documents"] == 0, \
        "2: HARD FAIL - the sync attached a file"
    assert r["batch_id"] is not None and ds.status(conn)["pending"] >= 4, "2: the review queue counts Drive files"

    # 3. a repeated check downloads nothing and stages nothing
    downloads, held = fake.downloads, counts(conn)
    run = ds.sync(conn, *D, now=T0)
    assert fake.downloads == downloads, "3: HARD FAIL - an unchanged file was downloaded again"
    assert counts(conn) == held and ds.run(conn, run)["new_files"] == 0, "3: a repeated check changed something"

    # 4. a new file placed in Drive is discovered, downloaded once, staged and proposed - never attached
    fake.add("id99", "D06-referto-gallo.txt", D06, "text/plain")
    run = ds.sync(conn, *D, now=T0)
    assert fake.downloads == downloads + 1 and ds.run(conn, run)["new_files"] == 1, "4: one new file, one download"
    new = [i for i in li.items(conn, None) if i["rel_path"] == "D06-referto-gallo.txt"]
    assert len(new) == 1 and new[0]["state"] == "proposed" and new[0]["patient_id"] == pids["marco"], \
        f"4: the new file is proposed for Marco Gallo ({[dict(n) for n in new]})"
    assert counts(conn)["patient_documents"] == 0, "4: HARD FAIL - a strong proposal attached itself"

    # 5. a changed file (new content under the same Drive id) is downloaded again and staged as new evidence
    fake.add("id99", "D06-referto-gallo.txt", D06 + b"Aggiunta DEMO\n", "text/plain")
    ds.sync(conn, *D, now=T0)
    assert fake.downloads == downloads + 2, "5: a changed file is fetched again"
    assert len([i for i in li.items(conn, None) if i["rel_path"] == "D06-referto-gallo.txt"]) == 2, \
        "5: the changed bytes are a second item, the first is kept"

    # 6. refused files: Google-native, too large, md5 mismatch, a name that tries to leave the mirror
    fake.add("idG", "nota google", b"", "application/vnd.google-apps.document")
    fake.add("idL", "grande.txt", b"x" * (ds.MAX_BYTES + 1), "text/plain")
    fake.add("idC", "corrotto.txt", b"Nota DEMO\n", "text/plain")
    fake.corrupt.add("idC")
    fake.add("idE", "../../fuori.txt", b"Nota DEMO fuori\n", "text/plain")
    before_dl = fake.downloads
    run = ds.sync(conn, *D, now=T0)
    r = ds.run(conn, run)
    assert r["status"] == "partial" and r["skipped"] >= 3, f"6: refusals are counted ({dict(r)})"
    assert not (mirror / "nota google").exists() and not (mirror / "grande.txt").exists() \
        and not (mirror / "corrotto.txt").exists(), "6: refused files are not written"
    assert not (tmp / "fuori.txt").exists() and not (mirror.parent / "fuori.txt").exists(), \
        "6: HARD FAIL - a Drive file name wrote outside the mirror"
    assert all(p.resolve().parent == mirror.resolve() for p in mirror.iterdir()), "6: everything stays in the mirror"
    assert fake.downloads - before_dl <= 2, "6: a Google-native or oversized file is not downloaded"
    for fid in ("idG", "idL", "idC", "idE"):
        del fake.files[fid]
    fake.corrupt.clear()

    # 7. revoked access (403) and a vanished folder (404): an error, nothing changed, the key nowhere
    held, held_mirror = counts(conn), sorted(p.name for p in mirror.iterdir())
    for code, expected in ((403, "refused"), (400, "refused"), (404, "not_found")):
        fake.deny = code
        run = ds.sync(conn, *D, now=T0)
        r = ds.run(conn, run)
        assert r["status"] == "error" and r["error_code"] == expected, f"7: {code} -> {dict(r)}"
        assert counts(conn) == held and sorted(p.name for p in mirror.iterdir()) == held_mirror, \
            f"7: HARD FAIL - a failed check ({code}) changed something"
    fake.deny = None
    assert ds.status(conn)["last"]["status"] == "error", "7: the last check shows the error"

    # 8. an interrupted check leaves a visible trace and the next one finishes without duplicates
    fake.add("id97", "D07-nota.txt", b"Nota DEMO 7\n", "text/plain")
    fake.add("id98", "D08-nota.txt", b"Nota DEMO 8\n", "text/plain")
    fake.crash_after = fake.downloads + 1
    try:
        ds.sync(conn, *D, now=T0)
        raise AssertionError("8: the interruption did not happen")
    except KeyboardInterrupt:
        pass
    fake.crash_after = None
    stuck = conn.execute("SELECT id, status FROM drive_sync_runs ORDER BY id DESC LIMIT 1").fetchone()
    assert stuck["status"] == "running", "8: an interrupted check is visibly unfinished"
    run = ds.sync(conn, *D, now=T0)
    assert ds.run(conn, stuck["id"])["status"] == "interrupted", "8: the next check marks it interrupted"
    names = [i["rel_path"] for i in li.items(conn, None)]
    assert names.count("D07-nota.txt") == 1 and names.count("D08-nota.txt") == 1, "8: resumed without duplicates"
    assert ds.run(conn, run)["status"] == "ok", "8: the resumed check finishes"

    # 8b. the network drops in the middle of a check: an error, what was fetched is kept, the next check finishes
    fake.add("id95", "D10-nota.txt", b"Nota DEMO 10\n", "text/plain")
    fake.add("id94", "D11-nota.txt", b"Nota DEMO 11\n", "text/plain")
    fake.drop_after = fake.downloads + 1
    try:
        run = ds.sync(conn, *D, now=T0)
    except ds.DriveError as e:
        raise AssertionError(f"8b: a dropped connection must end the check as an error, not escape it ({e.code})")
    r = ds.run(conn, run)
    assert r["status"] == "error" and r["error_code"] == "network", f"8b: a dropped connection is an error ({dict(r)})"
    fake.drop_after = None
    run = ds.sync(conn, *D, now=T0)
    names = [i["rel_path"] for i in li.items(conn, None)]
    assert ds.run(conn, run)["status"] == "ok" and names.count("D10-nota.txt") == 1 and names.count("D11-nota.txt") == 1, \
        "8b: the next check finishes the interrupted one, without duplicates"

    # 9. no marker in the Drive folder: listed, nothing downloaded
    marker = next(fid for fid, f in fake.files.items() if f["name"] == li.MARKER)
    saved = fake.files.pop(marker)
    dl = fake.downloads
    fake.add("id96", "D09-nota.txt", b"Nota DEMO 9\n", "text/plain")
    run = ds.sync(conn, *D, now=T0)
    assert ds.run(conn, run)["error_code"] == "not_authorised" and fake.downloads == dl, \
        "9: HARD FAIL - a folder without the synthetic marker was downloaded"
    fake.files[marker] = saved
    del fake.files["id96"]

    # 10. read-only by construction: GET only, the Drive API host only, downloads only of listed ids
    assert {m for m, _u in fake.calls} == {"GET"}, "10: HARD FAIL - a request other than GET"
    assert all(urllib.parse.urlparse(u).netloc == "www.googleapis.com" for _m, u in fake.calls), "10: another host"
    listed = set(fake.files) | {"idG", "idL", "idC", "idE", "id96", "id97", "id98", "id99", "id95", "id94"}
    for _m, u in fake.calls:
        if "alt=media" in u:
            assert urllib.parse.urlparse(u).path.rsplit("/", 1)[-1] in listed, "10: a file that was not listed"

    # 11. roles: only a dentist syncs; refusals audited; the key appears nowhere in the database
    for who in (A, ADM):
        try:
            ds.sync(conn, *who, now=T0)
            raise AssertionError(f"11: {who[1]} synced")
        except PermissionError:
            pass
    dump = "\n".join(str(tuple(r)) for t in ("audit_log", "drive_sync_runs", "drive_files", "drive_settings")
                     for r in conn.execute(f"SELECT * FROM {t}"))
    assert KEY not in dump, "11: HARD FAIL - the key is stored in the database"
    return fake


def auto(conn, fake):
    # 12. the automatic check: off by default, bounded interval, one at a time, stops after repeated errors
    runs = conn.execute("SELECT COUNT(*) FROM drive_sync_runs").fetchone()[0]
    try:
        off = ds.auto_tick(conn, T0)
    except Exception as e:
        raise AssertionError(f"12: the automatic check ran while off ({type(e).__name__})")
    assert off is None and conn.execute("SELECT COUNT(*) FROM drive_sync_runs").fetchone()[0] == runs, "12: off by default"
    for asked, got in ((1, 5), (5, 5), (15, 15), (999, 60)):
        ds.set_auto(conn, True, asked, *D, now=T0)
        assert ds.settings(conn)["interval_min"] == got, f"12: interval {asked} -> {got}"
    ds.set_auto(conn, True, 5, *D, now=T0)
    first = ds.auto_tick(conn, T0)
    assert first is not None and ds.run(conn, first)["trigger"] == "auto", "12: due at once when turned on"
    assert ds.auto_tick(conn, T0.replace(minute=2)) is None, "12: not again within the interval"
    later = T0.replace(minute=6)
    assert ds.auto_tick(conn, later) is not None, "12: again after the interval"
    with ds.LOCK:
        assert ds.auto_tick(conn, T0.replace(minute=20)) is None, "12: never two checks at once"
    fake.deny = 403
    for i in range(3):
        ds.auto_tick(conn, T0.replace(hour=9 + i))
    s = ds.settings(conn)
    assert not s["auto_enabled"] and "error" in (s["disabled_reason"] or ""), "12: three errors turn it off"
    fake.deny = None
    try:
        ds.set_auto(conn, True, 5, *A, now=T0)
        raise AssertionError("12: reception turned the automatic check on")
    except PermissionError:
        pass


def routes(tmp, conn, pids, app, db_path):
    dentist = staff_client(app, db_path, *D)
    assistant = staff_client(app, db_path, *A)
    admin = staff_client(app, db_path, *ADM)
    # 13. the dentist sees the source card with status, Sync now and the automatic check
    page = dentist.get("/imports").text
    assert "Google Drive source" in page and "Sync now" in page and "Automatic check" in page, "13: the card"
    assert FOLDER in page and "Last check" in page and "waiting for your review" in page.lower(), "13: status shown"
    assert KEY not in page, "13: HARD FAIL - the key is on the page"
    # 14. reception and admin: no card, and the actions refuse and are audited
    ap = assistant.get("/imports").text
    assert "Google Drive source" not in ap and FOLDER not in ap, "14: reception sees no Drive source"
    before = counts(conn)
    audited = conn.execute("SELECT COUNT(*) FROM audit_log WHERE action IN ('drive_sync', 'drive_auto')"
                           " AND allowed = 0").fetchone()[0]
    for client in (assistant, admin):
        token = csrf(client.get("/", follow_redirects=True).text)
        for path, data in (("/imports/drive/sync", {}), ("/imports/drive/auto", {"enabled": "1", "interval": "5"})):
            try:
                r = client.post(path, data={"csrf_token": token, **data})
            except PermissionError:
                raise AssertionError(f"14: {path} was not refused at the route; only the service stopped it")
            assert r.status_code == 302, f"14: {path} not refused ({r.status_code})"
    assert counts(conn) == before, "14: HARD FAIL - a refused action changed something"
    refused = conn.execute("SELECT COUNT(*) FROM audit_log WHERE action IN ('drive_sync', 'drive_auto')"
                           " AND allowed = 0").fetchone()[0]
    assert refused - audited == 4, f"14: each refusal is audited ({refused - audited} of 4)"
    # 15. csrf, then Sync now from the page
    assert dentist.post("/imports/drive/sync", data={}).status_code == 400, "15: no csrf, no sync"
    r = dentist.post("/imports/drive/sync", data={"csrf_token": csrf(page)})
    assert r.status_code == 302, "15: Sync now runs from the page"
    page = dentist.get("/imports").text
    card = page.split('id="drive"', 1)[1].split("</section>", 1)[0]
    assert "Last check" in card and "finished" in card, "15: the result of the check is shown on the card"
    r = dentist.post("/imports/drive/auto", data={"csrf_token": csrf(page), "enabled": "0", "interval": "5"})
    assert r.status_code == 302 and not ds.settings(conn)["auto_enabled"], "15: the automatic check turns off"
    # 16. confirmation stays the only way in: the new Drive file onto Marco Gallo's record, not on the portal
    it = next(i for i in li.items(conn, None) if i["rel_path"] == "D06-referto-gallo.txt" and i["state"] == "proposed")
    cf = conn.execute("SELECT codice_fiscale FROM patients WHERE patient_id = ?", (pids["marco"],)).fetchone()[0]
    page = dentist.get(f"/imports/items/{it['id']}").text
    r = dentist.post(f"/imports/items/{it['id']}/confirm", data={"csrf_token": csrf(page), "patient_cf": cf,
                                                                 "expected_sha": it["sha256"], "reason": "", "visit_id": ""})
    assert r.status_code == 302 and li.item(conn, it["id"])["state"] == "confirmed", "16: confirmed by the dentist"
    assert conn.execute("SELECT COUNT(*) FROM patient_documents WHERE patient_id = ?", (pids["marco"],)).fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM document_publications").fetchone()[0] == 0, \
        "16: HARD FAIL - confirming published the file"
    assert "D06-referto-gallo.txt" in dentist.get(f"/patients/{cf}").text, "16: on Marco Gallo's record"


def over_http(tmp, conn):
    # 17. the real urllib path against a local HTTP server: list, download, 403 mapping, key only as a parameter
    seen = []

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if self.path.startswith("/deny"):
                body, code = b'{"error": {"code": 403, "message": "nope"}}', 403
            elif q.get("alt") == ["media"]:
                body, code = b"Nota DEMO http\n", 200
            else:
                body, code = json.dumps({"files": [{"id": "h1", "name": "h.txt", "mimeType": "text/plain",
                                                    "size": "15", "md5Checksum": "x"}]}).encode(), 200
            self.send_response(code)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        status, body = ds.http_get(f"{base}/drive/v3/files?q=x&key={KEY}", 5)
        assert status == 200 and json.loads(body)["files"][0]["id"] == "h1", "17: list over HTTP"
        status, body = ds.http_get(f"{base}/drive/v3/files/h1?alt=media&key={KEY}", 5)
        assert status == 200 and body == b"Nota DEMO http\n", "17: download over HTTP"
        status, _ = ds.http_get(f"{base}/deny?key={KEY}", 5)
        assert status == 403, "17: an HTTP error comes back as its status, not an exception"
        try:
            ds.http_get("http://127.0.0.1:9/drive/v3/files?key=" + KEY, 2)
            raise AssertionError("17: a closed port answered")
        except ds.DriveError as e:
            assert e.code == "network" and KEY not in str(e), "17: HARD FAIL - the key leaked into an error"
    finally:
        srv.shutdown()


def selftest():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        db_path = str(tmp / "clinic.sqlite")
        app_db.DB_PATH = db_path
        app_db.CHROMA_PATH = str(tmp / "chroma")
        docs.DOC_ROOT = tmp / "documents"
        docs.DOC_CHROMA_PATH = str(tmp / "doc_chroma")
        docs.SLOT_DIR = tmp / "slots"
        docs.unindex = lambda ids: None
        li.STAGING_ROOT = tmp / "import_staging"
        li.INBOX = tmp / "import_inbox"
        ds.MIRROR_ROOT = tmp / "import_drive"
        from app import create_app
        app = create_app()
        app.config["TESTING"] = True
        from storage import connect
        conn = connect(db_path)
        conn.row_factory = sqlite3.Row
        pids = {k: patient_id.seed_patient(conn, code, name, None) for k, (code, name) in fx.DRIVE_PEOPLE.items()}
        conn.commit()
        fake = service(tmp, conn, pids)
        auto(conn, fake)
        routes(tmp, conn, pids, app, db_path)
        over_http(tmp, conn)
        conn.close()
    print("drive_source_selftest: ok")


if __name__ == "__main__":
    selftest()
    sys.exit(0)
