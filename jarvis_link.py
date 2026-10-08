"""The clinic side of Jarvis (J00): registered devices and staff sessions delegated to them; since J02 also the device's
own clinic-guide questions.

A Jarvis device proves only that it is a device the admin registered: a random credential, shown once, stored on the
device and kept here as a hash, sent over loopback. Anything protected also needs a staff member who is signed in to
the clinic app right now and has delegated *that* session to *that* device. The device never decides access: every
check is here, every call is audited, and voice plays no part in it.
"""
import re
import secrets
from datetime import timedelta

import clinic_time
import web_session
from auth import authorize, log_audit

DELEGATION_HOURS = 8          # a delegation never outlives this, even if the staff session stays alive
# a device asks clinic guides as reception does - the least-privileged role that may read them. A delegation never
# widens this in J02: dentist-only and restricted pages are refused through Jarvis whoever has delegated a session.
GUIDE_ROLE = "assistant"
MAX_QUESTION = 500
LAST_SHOWN_TOKEN = None       # the credential of the last registration, for the one page that shows it (and tests)

SCHEMA = """
    CREATE TABLE IF NOT EXISTS jarvis_devices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        token_hash TEXT NOT NULL UNIQUE,
        registered_by TEXT NOT NULL,
        registered_at TEXT NOT NULL,
        revoked_by TEXT,
        revoked_at TEXT
    );
    CREATE TABLE IF NOT EXISTS jarvis_delegations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id INTEGER NOT NULL REFERENCES jarvis_devices(id),
        username TEXT NOT NULL,
        session_hash TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        revoked_by TEXT,
        revoked_at TEXT
    );
"""


class LinkError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _now(now):
    return now or clinic_time.now_utc()


def _require(conn, actor, role, capability, action, target):
    if not authorize(role, capability):
        log_audit(conn, actor, role, action, target, allowed=0)
        raise PermissionError(f"{role} may not {action.replace('_', ' ')}")


# --- devices (admin) -------------------------------------------------------------------------------

def register_device(conn, name, actor, role, now=None):
    """-> (device id, credential). The credential is returned once and never stored in clear."""
    global LAST_SHOWN_TOKEN
    _require(conn, actor, role, "manage_users", "jarvis_device_register", "jarvis")
    name = (name or "").strip()[:60]
    if not name:
        raise LinkError("name", "give the device a name")
    token = secrets.token_urlsafe(32)
    cur = conn.execute("INSERT INTO jarvis_devices (name, token_hash, registered_by, registered_at) VALUES (?, ?, ?, ?)",
                       (name, web_session.token_hash(token), actor, clinic_time.to_storage(_now(now))))
    conn.commit()
    log_audit(conn, actor, role, "jarvis_device_register", f"jarvis_device:{cur.lastrowid}", allowed=1)
    LAST_SHOWN_TOKEN = token
    return cur.lastrowid, token


def revoke_device(conn, device_id, actor, role, now=None):
    _require(conn, actor, role, "manage_users", "jarvis_device_revoke", f"jarvis_device:{device_id}")
    stamp = clinic_time.to_storage(_now(now))
    conn.execute("UPDATE jarvis_devices SET revoked_by = ?, revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                 (actor, stamp, device_id))
    conn.execute("UPDATE jarvis_delegations SET revoked_by = ?, revoked_at = ? WHERE device_id = ? AND revoked_at IS NULL",
                 (actor, stamp, device_id))
    conn.commit()
    log_audit(conn, actor, role, "jarvis_device_revoke", f"jarvis_device:{device_id}", allowed=1)


def devices(conn):
    return conn.execute("SELECT id, name, registered_by, registered_at, revoked_at FROM jarvis_devices ORDER BY id").fetchall()


# --- delegations (dentist, reception) ---------------------------------------------------------------

def delegate(conn, device_id, session_token, actor, role, now=None):
    """The actor's own live session, bound to one active device. -> delegation id."""
    _require(conn, actor, role, "use_jarvis", "jarvis_delegate", f"jarvis_device:{device_id}")
    now = _now(now)
    live = web_session.session_alive(conn, web_session.token_hash(session_token or ""), now)
    if live is None or live["username"] != actor:
        log_audit(conn, actor, role, "jarvis_delegate", f"jarvis_device:{device_id}", allowed=0)
        raise LinkError("session", "only your own live session can be used for Jarvis")
    device = conn.execute("SELECT id FROM jarvis_devices WHERE id = ? AND revoked_at IS NULL", (device_id,)).fetchone()
    if device is None:
        raise LinkError("device", "that device is not registered or was revoked")
    stamp = clinic_time.to_storage(now)
    # one delegation per device at a time: a new one replaces the previous holder's
    conn.execute("UPDATE jarvis_delegations SET revoked_by = ?, revoked_at = ? WHERE device_id = ? AND revoked_at IS NULL",
                 (actor, stamp, device_id))
    cur = conn.execute("INSERT INTO jarvis_delegations (device_id, username, session_hash, created_at, expires_at)"
                       " VALUES (?, ?, ?, ?, ?)", (device_id, actor, web_session.token_hash(session_token), stamp,
                                                   clinic_time.to_storage(now + timedelta(hours=DELEGATION_HOURS))))
    conn.commit()
    log_audit(conn, actor, role, "jarvis_delegate", f"jarvis_device:{device_id}", allowed=1)
    return cur.lastrowid


def revoke_delegation(conn, delegation_id, actor, role, now=None):
    _require(conn, actor, role, "use_jarvis", "jarvis_delegation_revoke", f"jarvis_delegation:{delegation_id}")
    row = conn.execute("SELECT username FROM jarvis_delegations WHERE id = ?", (delegation_id,)).fetchone()
    if row is None or row["username"] != actor:
        raise LinkError("lookup", "no such delegation of yours")
    conn.execute("UPDATE jarvis_delegations SET revoked_by = ?, revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                 (actor, clinic_time.to_storage(_now(now)), delegation_id))
    conn.commit()
    log_audit(conn, actor, role, "jarvis_delegation_revoke", f"jarvis_delegation:{delegation_id}", allowed=1)


def delegations_for(conn, username):
    return conn.execute("SELECT g.id, g.device_id, d.name, g.created_at, g.expires_at FROM jarvis_delegations g"
                        " JOIN jarvis_devices d ON d.id = g.device_id WHERE g.username = ? AND g.revoked_at IS NULL"
                        " AND d.revoked_at IS NULL ORDER BY g.id DESC", (username,)).fetchall()


# --- the device's view ------------------------------------------------------------------------------

def authenticate(conn, bearer):
    """-> the active device for this credential, or LinkError. Says nothing about which part was wrong."""
    if not bearer or len(bearer) < 20:
        raise LinkError("device", "not a registered Jarvis device")
    row = conn.execute("SELECT * FROM jarvis_devices WHERE token_hash = ? AND revoked_at IS NULL",
                       (web_session.token_hash(bearer),)).fetchone()
    if row is None:
        raise LinkError("device", "not a registered Jarvis device")
    return row


def current_delegation(conn, device_id, now=None):
    """-> {username, role, expires_at} while the delegation, its staff session and its account are all valid."""
    now = _now(now)
    row = conn.execute("SELECT * FROM jarvis_delegations WHERE device_id = ? AND revoked_at IS NULL ORDER BY id DESC"
                       " LIMIT 1", (device_id,)).fetchone()
    if row is None or now >= clinic_time.read_instant(row["expires_at"]):
        return None
    live = web_session.session_alive(conn, row["session_hash"], now)
    if live is None or live["username"] != row["username"]:
        return None
    # the role is the account's current one, never a copy taken when the delegation was made
    return {"username": row["username"], "role": live["role"], "expires_at": row["expires_at"]}


def whoami(conn, bearer, now=None):
    device = authenticate(conn, bearer)
    return {"device": device["name"], "device_id": device["id"], "delegation": current_delegation(conn, device["id"], now)}


# --- clinic-guide questions (J02) ---------------------------------------------------------------------------

NUMBER_WORDS = {"zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
                "seven": "7", "eight": "8", "nine": "9", "ten": "10", "hundred": "00",
                "uno": "1", "due": "2", "tre": "3", "quattro": "4", "cinque": "5", "sei": "6", "sette": "7",
                "otto": "8", "nove": "9", "dieci": "10", "cento": "00"}
WORD = re.compile(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*")
MAX_SPAN = 4


def _bare(code):
    return code.replace("-", "").upper()


def library_codes(gconn):
    """{code without hyphens: code} for registered device models and the codes on approved pages - letters with a digit
    or a hyphen (B-PROG, E05, AX-300). A form two codes share is left out: it could not be matched safely."""
    import clinic_guides as cg
    found = [d["model"] for d in cg.devices(gconn)]
    for row in gconn.execute("SELECT p.text, p.ocr_text FROM pages p JOIN sources s ON s.id = p.source_id"
                             " WHERE s.status = 'approved'"):
        found += WORD.findall(f"{row['text'] or ''} {row['ocr_text'] or ''}")
    codes, clash = {}, set()
    for code in found:
        code = code.upper()
        if not re.search(r"[A-Z]", code) or not re.search(r"[0-9-]", code):
            continue
        if codes.get(_bare(code), code) != code:
            clash.add(_bare(code))
        codes[_bare(code)] = code
    return {bare: code for bare, code in codes.items() if bare not in clash}


def match_codes(text, codes):
    """A spoken code split by speech to text ("A X 300", "B prog", "E zero five") becomes the library's own code.
    Only spans that join to a library code are touched; every other word stays exactly as heard."""
    words = list(WORD.finditer(text))
    out, at, i = [], 0, 0
    while i < len(words):
        for n in range(min(MAX_SPAN, len(words) - i), 0, -1):
            span = words[i:i + n]
            if any(text[a.end():b.start()].strip() for a, b in zip(span, span[1:])):
                continue                                  # a comma or a full stop between them: not one code
            joined = "".join(NUMBER_WORDS.get(w.group().lower(), w.group()) for w in span)
            code = codes.get(_bare(joined))
            if code and (n > 1 or span[0].group() != code):
                out.append(text[at:span[0].start()] + code)
                at, i = span[-1].end(), i + n
                break
        else:
            i += 1
    return "".join(out) + text[at:]


MAX_TERMS = 80


def vocabulary(gconn, conn, role=GUIDE_ROLE):
    """The speech-to-text hint (J-D10): codes and capital-letter labels from the current, approved pages this role may
    read - never a superseded, withdrawn or pending document, a page for another audience, a restricted page or an
    unreadable page's OCR. Anything shaped like a codice fiscale and any word of a patient's name are left out (the
    names are read only for that and never leave this function). Computed on every call."""
    import clinic_guides as cg
    sql = ("SELECT p.text, p.ocr_text, p.readable FROM pages p JOIN sources s ON s.id = p.source_id"
           " WHERE s.status = 'approved'")
    if role != "dentist":
        sql += " AND s.audience = 'staff' AND p.restricted = 0"
    found = set()
    for row in gconn.execute(sql):
        text = row["text"] + (" " + row["ocr_text"] if row["readable"] else "")
        for word in WORD.findall(text):
            if re.search(r"[A-Za-z]", word) and re.search(r"[0-9-]", word):
                found.add(word.upper())                 # a code, whole: AX-300, E05, B-PROG
            elif len(word) > 1 and word.isalpha() and word.isupper():
                found.add(word)                         # a label printed in capitals: DRY, MODE
    names = set()
    for (name,) in conn.execute("SELECT patient_name FROM patients"):
        names.update(w.upper() for w in re.findall(r"[^\W\d_]{3,}", name or ""))
    terms = sorted(t for t in found if t not in names and not cg.CF_SHAPE.search(t))
    return terms[:MAX_TERMS]


def ask_guides(gconn, device, spoken):
    """An authenticated device's spoken question -> clinic_guides' answer or refusal under the device's guide scope.
    The question is never stored here (clinic_guides logs the outcome and citations only)."""
    import clinic_guides as cg
    question = match_codes(" ".join(spoken.split())[:MAX_QUESTION], library_codes(gconn))
    result = cg.ask(gconn, question, GUIDE_ROLE, actor=f"jarvis-device:{device['id']}")
    keep = ("outcome", "reason", "message", "escalation", "citations", "warnings", "conflicting", "see")
    out = {"asked_as": question, **{k: result[k] for k in keep if k in result}}
    d = result.get("device")
    out["device"] = {"make": d["make"], "model": d["model"], "room": d["room"]} if d else None
    if result["reason"] == "ask_device":
        out["devices"] = [{"make": r["make"], "model": r["model"], "room": r["room"]} for r in cg.devices(gconn)]
    return out
