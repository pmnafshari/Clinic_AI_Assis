"""Read-only Google Drive source for the legacy import (DRV, P25-D4: one folder of synthetic test files).

The app lists one approved folder through the Google Drive API v3 and downloads each new or changed file into its
own mirror, then hands the mirror to legacy_import.stage: the same marker guard, evidence gates, staging and review.
Nothing is attached here; a dentist still confirms every file, and publication stays a separate dentist action.

Read-only by construction: every request is a GET to www.googleapis.com, an API key cannot write, and only ids that
were just listed in the configured folder are downloaded. The key lives outside the repository and is never logged,
stored, shown or put in an error message.

    CLINIC_DRIVE_FOLDER_ID   the one folder (owner decision P25-D4)
    CLINIC_DRIVE_KEY_FILE    default ~/.config/clinic-demo/drive-api-key (or CLINIC_DRIVE_API_KEY)
"""
import hashlib
import json
import os
import re
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import clinic_time
import legacy_import as li
from auth import authorize, log_audit

API_BASE = "https://www.googleapis.com/drive/v3"
FOLDER_ID = os.environ.get("CLINIC_DRIVE_FOLDER_ID", "")
KEY_FILE = Path(os.environ.get("CLINIC_DRIVE_KEY_FILE", "~/.config/clinic-demo/drive-api-key")).expanduser()
MIRROR_ROOT = Path("import_drive")
MAX_LISTED = 200          # files looked at in one check
MAX_DOWNLOADS = 20        # files fetched in one check; the rest wait for the next one
MAX_BYTES = 10 * 1024 * 1024
TIMEOUT = 20
INTERVALS = (5, 60)       # minutes, the automatic check's bounds
TICK_SECONDS = 30
STOP_AFTER_ERRORS = 3
NATIVE = "application/vnd.google-apps."
LOCK = threading.Lock()

SCHEMA = """
    CREATE TABLE IF NOT EXISTS drive_sync_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        folder_id TEXT NOT NULL,
        started_by TEXT NOT NULL,
        trigger TEXT NOT NULL CHECK (trigger IN ('manual', 'auto')),
        started_at TEXT NOT NULL,
        finished_at TEXT,
        status TEXT NOT NULL CHECK (status IN ('running', 'ok', 'partial', 'error', 'interrupted')),
        listed INTEGER NOT NULL DEFAULT 0,
        new_files INTEGER NOT NULL DEFAULT 0,
        skipped INTEGER NOT NULL DEFAULT 0,
        error_code TEXT,
        error_message TEXT,
        batch_id INTEGER
    );
    CREATE TABLE IF NOT EXISTS drive_files (
        drive_id TEXT PRIMARY KEY,
        folder_id TEXT NOT NULL,
        name TEXT NOT NULL,
        rel TEXT NOT NULL,
        md5 TEXT,
        size INTEGER,
        modified_time TEXT,
        sha256 TEXT,
        first_seen_at TEXT NOT NULL,
        downloaded_at TEXT,
        last_run_id INTEGER
    );
    CREATE TABLE IF NOT EXISTS drive_settings (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        auto_enabled INTEGER NOT NULL DEFAULT 0,
        interval_min INTEGER NOT NULL DEFAULT 5,
        enabled_by TEXT,
        enabled_at TEXT,
        consecutive_errors INTEGER NOT NULL DEFAULT 0,
        disabled_reason TEXT
    );
"""

STATUS_LABELS = {"running": "running", "ok": "finished", "partial": "finished with files skipped",
                 "error": "failed", "interrupted": "interrupted - the next check resumes it"}


class DriveError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


# --- the connection -----------------------------------------------------------------------------------------

def _key():
    env = os.environ.get("CLINIC_DRIVE_API_KEY", "").strip()
    if env:
        return env
    try:
        return Path(KEY_FILE).read_text().strip() or None
    except OSError:
        return None


def configured():
    """-> (ok, reason). The reason never contains the key."""
    if not FOLDER_ID:
        return False, "no folder is configured (CLINIC_DRIVE_FOLDER_ID)"
    if not _key():
        return False, f"no API key: put it in {KEY_FILE} (outside the repository)"
    return True, ""


def http_get(url, timeout):
    """One GET. -> (status, body). Network trouble is a DriveError whose text never carries the url (it holds the key)."""
    req = urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as e:
        return e.code, e.read(64 * 1024)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        reason = getattr(e, "reason", e)
        raise DriveError("network", f"Drive could not be reached ({type(reason).__name__})") from None


def _transport(method, url, timeout):
    assert method == "GET"
    return http_get(url, timeout)


TRANSPORT = _transport


def _call(path, params, key):
    url = f"{API_BASE}{path}?" + urllib.parse.urlencode({**params, "key": key})
    status, body = TRANSPORT("GET", url, TIMEOUT)
    if status == 400 and b"API_KEY_INVALID" in body:
        status = 403  # a deleted or invalid key: Drive says 400, but for the clinic it is refused access
    if status in (401, 403):
        raise DriveError("refused", "Drive refused access: the key was revoked, restricted or the folder is no"
                                    " longer shared with it")
    if status == 404:
        raise DriveError("not_found", "Drive did not find the folder or the file")
    if status != 200:
        raise DriveError("http", f"Drive answered {status}")
    return body


def list_folder(key):
    files, token = [], None
    while True:
        params = {"q": f"'{FOLDER_ID}' in parents and trashed=false", "pageSize": 100,
                  "fields": "nextPageToken,files(id,name,mimeType,size,md5Checksum,modifiedTime)"}
        if token:
            params["pageToken"] = token
        page = json.loads(_call("/files", params, key))
        files.extend(page.get("files", []))
        token = page.get("nextPageToken")
        if not token or len(files) >= MAX_LISTED:
            return files[:MAX_LISTED]


def download(file_id, key):
    return _call(f"/files/{urllib.parse.quote(file_id)}", {"alt": "media"}, key)


# --- the mirror -----------------------------------------------------------------------------------------------

def mirror_dir():
    return Path(MIRROR_ROOT) / re.sub(r"[^A-Za-z0-9_-]", "_", FOLDER_ID or "none")


def safe_name(name):
    """A Drive name as one plain file name inside the mirror: no folders, no way out."""
    if name == li.MARKER:
        return name
    clean = re.sub(r"[^A-Za-z0-9 ._()-]", "_", name).strip(" .") or "file"
    return clean[:120]


def _write_atomic(folder, rel, data):
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".drive-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, folder / rel)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# --- one check ------------------------------------------------------------------------------------------------

def _now(now):
    return clinic_time.to_storage(now or clinic_time.now_utc())


def _require(conn, actor, role, action):
    if not authorize(role, li.REVIEW):
        log_audit(conn, actor, role, action, "drive", allowed=0)
        raise PermissionError(f"{role} may not {action.replace('_', ' ')}")


def run(conn, run_id):
    return conn.execute("SELECT * FROM drive_sync_runs WHERE id = ?", (run_id,)).fetchone()


def _finish(conn, run_id, status, now, **fields):
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE drive_sync_runs SET status = ?, finished_at = ?{', ' + sets if sets else ''} WHERE id = ?",
                 (status, _now(now), *fields.values(), run_id))
    conn.commit()


def sync(conn, actor, role, now=None, trigger="manual"):
    """List the folder, fetch what is new or changed, stage it. -> run id. Attaches nothing."""
    _require(conn, actor, role, "drive_sync")
    ok, reason = configured()
    if not ok:
        raise DriveError("not_configured", reason)
    if not LOCK.acquire(blocking=False):
        raise DriveError("busy", "a check is already running")
    try:
        return _sync(conn, actor, role, now, trigger)
    finally:
        LOCK.release()


def _sync(conn, actor, role, now, trigger):
    # a run a crash left 'running' is over: say so, then this one resumes its work
    conn.execute("UPDATE drive_sync_runs SET status = 'interrupted', finished_at = ? WHERE status = 'running'",
                 (_now(now),))
    run_id = conn.execute("INSERT INTO drive_sync_runs (folder_id, started_by, trigger, started_at, status)"
                          " VALUES (?, ?, ?, ?, 'running')", (FOLDER_ID, actor, trigger, _now(now))).lastrowid
    conn.commit()
    log_audit(conn, actor, role, "drive_sync", f"drive_run:{run_id}", allowed=1)
    key = _key()
    try:
        listed = list_folder(key)
    except DriveError as e:
        _finish(conn, run_id, "error", now, error_code=e.code, error_message=str(e))
        return run_id
    if not any(f.get("name") == li.MARKER for f in listed):
        _finish(conn, run_id, "error", now, listed=len(listed), error_code="not_authorised",
                error_message="The Drive folder has no synthetic-test marker; nothing was downloaded (P25-D1).")
        return run_id
    try:
        new, skipped, problems = _fetch_new(conn, run_id, listed, key, now)
    except DriveError as e:
        # what was fetched stays in the mirror; the next check stages it and fetches the rest
        fetched = conn.execute("SELECT COUNT(*) FROM drive_files WHERE last_run_id = ?", (run_id,)).fetchone()[0]
        _finish(conn, run_id, "error", now, listed=len(listed), new_files=fetched, error_code=e.code,
                error_message=str(e))
        return run_id
    batch = li.stage(conn, mirror_dir(), actor, role, now=now)
    _finish(conn, run_id, "partial" if skipped else "ok", now, listed=len(listed), new_files=new, skipped=skipped,
            batch_id=batch, error_message="; ".join(problems)[:500] or None)
    return run_id


def _fetch_new(conn, run_id, listed, key, now):
    """Download each listed file whose md5 is new, verify it, write it to the mirror. -> (new, skipped, problems)."""
    folder = mirror_dir()
    names = [safe_name(f["name"]) for f in listed]
    new, skipped, problems, fetched = 0, 0, [], 0
    for f, rel in zip(listed, names):
        if names.count(rel) > 1:
            stem, dot, ext = rel.rpartition(".")
            rel = f"{stem}-{f['id'][:8]}.{ext}" if dot else f"{rel}-{f['id'][:8]}"
        known = conn.execute("SELECT md5, rel FROM drive_files WHERE drive_id = ?", (f["id"],)).fetchone()
        if known and known["md5"] == f.get("md5Checksum") and (folder / known["rel"]).is_file():
            continue
        if (f.get("mimeType") or "").startswith(NATIVE):
            skipped += 1
            problems.append(f"{rel}: a Google Docs-format file is not imported")
            continue
        if int(f.get("size") or 0) > MAX_BYTES:
            skipped += 1
            problems.append(f"{rel}: larger than {MAX_BYTES // (1024 * 1024)} MB")
            continue
        if fetched >= MAX_DOWNLOADS:
            continue  # the next check fetches the rest
        data = download(f["id"], key)
        fetched += 1
        if hashlib.md5(data).hexdigest() != f.get("md5Checksum") or len(data) > MAX_BYTES:
            skipped += 1
            problems.append(f"{rel}: the downloaded bytes do not match Drive's checksum")
            continue
        _write_atomic(folder, rel, data)
        conn.execute("INSERT INTO drive_files (drive_id, folder_id, name, rel, md5, size, modified_time, sha256,"
                     " first_seen_at, downloaded_at, last_run_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                     " ON CONFLICT(drive_id) DO UPDATE SET name = excluded.name, rel = excluded.rel,"
                     " md5 = excluded.md5, size = excluded.size, modified_time = excluded.modified_time,"
                     " sha256 = excluded.sha256, downloaded_at = excluded.downloaded_at,"
                     " last_run_id = excluded.last_run_id",
                     (f["id"], FOLDER_ID, f["name"], rel, f.get("md5Checksum"), len(data), f.get("modifiedTime"),
                      hashlib.sha256(data).hexdigest(), _now(now), _now(now), run_id))
        conn.commit()
        new += 1
    return new, skipped, problems


# --- status and the automatic check --------------------------------------------------------------------------

def settings(conn):
    row = conn.execute("SELECT * FROM drive_settings WHERE id = 1").fetchone()
    if row is None:
        # an INSERT opens a transaction even when it changes nothing, so write only when the row is missing
        conn.execute("INSERT OR IGNORE INTO drive_settings (id) VALUES (1)")
        conn.commit()
        row = conn.execute("SELECT * FROM drive_settings WHERE id = 1").fetchone()
    return row


def set_auto(conn, enabled, interval_min, actor, role, now=None):
    _require(conn, actor, role, "drive_auto")
    settings(conn)
    minutes = min(max(int(interval_min or INTERVALS[0]), INTERVALS[0]), INTERVALS[1])
    conn.execute("UPDATE drive_settings SET auto_enabled = ?, interval_min = ?, enabled_by = ?, enabled_at = ?,"
                 " consecutive_errors = 0, disabled_reason = NULL WHERE id = 1",
                 (1 if enabled else 0, minutes, actor, _now(now)))
    conn.commit()
    log_audit(conn, actor, role, "drive_auto", "drive", allowed=1, reason="on" if enabled else "off")


def auto_tick(conn, now=None):
    """Called by the staff app's timer. Runs a check when the automatic check is on and due. -> run id or None."""
    s = settings(conn)
    if not s["auto_enabled"] or not configured()[0]:
        return None
    now = now or clinic_time.now_utc()
    last = conn.execute("SELECT started_at FROM drive_sync_runs WHERE trigger = 'auto' ORDER BY id DESC LIMIT 1"
                        ).fetchone()
    if last and (now - clinic_time.read_instant(last[0])).total_seconds() < s["interval_min"] * 60:
        return None
    try:
        run_id = sync(conn, s["enabled_by"], "dentist", now=now, trigger="auto")
    except DriveError as e:
        if e.code == "busy":
            return None
        raise
    failed = run(conn, run_id)["status"] == "error"
    errors = s["consecutive_errors"] + 1 if failed else 0
    if errors >= STOP_AFTER_ERRORS:
        conn.execute("UPDATE drive_settings SET auto_enabled = 0, consecutive_errors = ?, disabled_reason = ?"
                     " WHERE id = 1", (errors, f"turned off after {errors} checks in a row ended in an error"))
    else:
        conn.execute("UPDATE drive_settings SET consecutive_errors = ? WHERE id = 1", (errors,))
    conn.commit()
    return run_id


def status(conn):
    ok, reason = configured()
    last = conn.execute("SELECT * FROM drive_sync_runs ORDER BY id DESC LIMIT 1").fetchone()
    key = li.source_key(mirror_dir())
    pending = conn.execute("SELECT COUNT(*) FROM import_items WHERE source_key = ? AND state IN"
                           f" ({','.join('?' * len(li.OPEN))})", (key, *li.OPEN)).fetchone()[0]
    files = conn.execute("SELECT COUNT(*) FROM drive_files WHERE folder_id = ?", (FOLDER_ID,)).fetchone()[0]
    return {"configured": ok, "reason": reason, "folder_id": FOLDER_ID, "last": last, "pending": pending,
            "files": files, "settings": settings(conn), "labels": STATUS_LABELS}


def start_auto_thread(db_path):
    """The staff app's timer (run.py only): wakes every TICK_SECONDS and runs auto_tick on its own connection."""
    from storage import connect
    stop = threading.Event()

    def loop():
        while not stop.wait(TICK_SECONDS):
            conn = connect(db_path)
            try:
                auto_tick(conn)
            except Exception as e:  # a bad tick must not kill the timer; the run row already says what failed
                print(f"drive auto check: {type(e).__name__}", file=sys.stderr)
            finally:
                conn.close()
    threading.Thread(target=loop, name="drive-auto-check", daemon=True).start()
    return stop
