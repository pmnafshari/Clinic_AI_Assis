"""The clinic side of Jarvis (J00): registered devices and staff sessions delegated to them; since J02 also the device's
own clinic-guide questions.

A Jarvis device proves only that it is a device the admin registered: a random credential, shown once, stored on the
device and kept here as a hash, sent over loopback. Anything protected also needs a staff member who is signed in to
the clinic app right now and has delegated *that* session to *that* device. The device never decides access: every
check is here, every call is audited, and voice plays no part in it.
"""
import collections
import re
import secrets
from datetime import datetime, time, timedelta

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
DOCTOR = "dentist"            # owner, 2026-10-11: only a doctor signs Jarvis in, and only a doctor lets it write


def ensure_columns(conn):
    """2026-10-11: a delegation is either a staff browser session handed to the device ('web', J00) or a doctor
    signing Jarvis in on the device itself ('login', until the next 00:00). Additive; old rows stay 'web'."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(jarvis_delegations)")}
    if "kind" not in cols:
        conn.execute("ALTER TABLE jarvis_delegations ADD COLUMN kind TEXT NOT NULL DEFAULT 'web'")
        conn.commit()


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
    """-> {username, role, expires_at, kind} while the delegation, its staff session (or, for a sign-in on the
    device, the doctor's account) and the account are all valid."""
    now = _now(now)
    row = conn.execute("SELECT * FROM jarvis_delegations WHERE device_id = ? AND revoked_at IS NULL ORDER BY id DESC"
                       " LIMIT 1", (device_id,)).fetchone()
    if row is None or now >= clinic_time.read_instant(row["expires_at"]):
        return None
    if row["kind"] == "login":          # signed in on the device: the account must still be an active doctor
        user = conn.execute("SELECT role, active FROM users WHERE username = ?", (row["username"],)).fetchone()
        if user is None or user["active"] != 1 or user["role"] != DOCTOR:
            return None
        return {"username": row["username"], "role": user["role"], "expires_at": row["expires_at"], "kind": "login"}
    live = web_session.session_alive(conn, row["session_hash"], now)
    if live is None or live["username"] != row["username"]:
        return None
    # the role is the account's current one, never a copy taken when the delegation was made
    return {"username": row["username"], "role": live["role"], "expires_at": row["expires_at"], "kind": "web"}


def next_midnight(now):
    """The next 00:00 in clinic time, as an instant: a sign-in on the device ends there, every day."""
    local = clinic_time.to_local(now)
    return clinic_time.to_utc(datetime.combine(local.date() + timedelta(days=1), time(0)))


def login(conn, device, username, password, now=None):
    """A doctor signs Jarvis in on the device with their own username and password - the clinic's own check, lockout
    and audit. Until the next 00:00. -> {username, role, expires_at}"""
    import web_auth
    now = _now(now)
    target = f"jarvis_device:{device['id']}"
    role = web_auth.attempt_login(username, password, conn)
    if role is None:
        log_audit(conn, username or "unknown", "unknown", "jarvis_signin", target, allowed=0, reason="credentials")
        raise LinkError("credentials", "wrong username or password")
    if role != DOCTOR:
        log_audit(conn, username, role, "jarvis_signin", target, allowed=0, reason="not_doctor")
        raise LinkError("not_doctor", "only a doctor can sign Jarvis in")
    owes = conn.execute("SELECT must_change_password FROM users WHERE username = ?", (username,)).fetchone()
    if owes and owes[0]:
        log_audit(conn, username, role, "jarvis_signin", target, allowed=0, reason="password_change")
        raise LinkError("password_change", "change your password in the clinic app first")
    stamp = clinic_time.to_storage(now)
    until = next_midnight(now)
    _end_handoff_sessions(conn, device["id"])
    conn.execute("UPDATE jarvis_delegations SET revoked_by = ?, revoked_at = ? WHERE device_id = ? AND revoked_at IS NULL",
                 (username, stamp, device["id"]))
    conn.execute("INSERT INTO jarvis_delegations (device_id, username, session_hash, created_at, expires_at, kind)"
                 " VALUES (?, ?, ?, ?, ?, 'login')", (device["id"], username, "", stamp, clinic_time.to_storage(until)))
    conn.commit()
    log_audit(conn, username, role, "jarvis_signin", target, allowed=1)
    return {"username": username, "role": role, "expires_at": clinic_time.to_storage(until)}


def _end_handoff_sessions(conn, device_id):
    """The clinic sessions handed over from this device's current sign-in end with it (one sign-in, 2026-10-12)."""
    for (h,) in conn.execute("SELECT session_hash FROM jarvis_delegations WHERE device_id = ? AND kind = 'login'"
                             " AND revoked_at IS NULL AND session_hash != ''", (device_id,)).fetchall():
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (h,))


def logout(conn, device, why):
    """Whoever Jarvis acts for on this device, it stops at once (sign out, or Jarvis starting again) - and so does the
    clinic session its sign-in handed to the Clinic tab."""
    _end_handoff_sessions(conn, device["id"])
    conn.execute("UPDATE jarvis_delegations SET revoked_by = ?, revoked_at = ? WHERE device_id = ? AND revoked_at IS NULL",
                 (f"jarvis-device:{device['id']}", clinic_time.to_storage(_now(None)), device["id"]))
    conn.commit()
    log_audit(conn, f"jarvis-device:{device['id']}", "device", "jarvis_signout", f"jarvis_device:{device['id']}",
              allowed=1, reason=why)


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
TERM = re.compile(r"[^\W_]+(?:-[^\W_]+)*")      # WORD in any alphabet: a label printed as PIÙ stays one word


def ranked_terms(gconn, conn, role=GUIDE_ROLE):
    """The speech-to-text hint (J-D10), every term in its order (J02 follow-up 6, R4): codes and capital-letter labels
    from the current, approved pages this role may read - never a superseded, withdrawn or pending document, a page for
    another audience, a restricted page or an unreadable page's OCR. Anything shaped like a codice fiscale and any word
    of a patient's name are left out (the names are read only for that and never leave this function). Computed on
    every call.

    Order, so a cap keeps the same terms whatever their letters: the registered device models first, then the terms
    more approved pages carry (a term asked about is likely on several), then alphabetically."""
    import clinic_guides as cg
    sql = ("SELECT p.text, p.ocr_text, p.readable FROM pages p JOIN sources s ON s.id = p.source_id"
           " WHERE s.status = 'approved'")
    if role != "dentist":
        sql += " AND s.audience = 'staff' AND p.restricted = 0"
    pages = collections.Counter()
    for row in gconn.execute(sql):
        text = row["text"] + (" " + row["ocr_text"] if row["readable"] else "")
        found = set()
        for word in TERM.findall(text):
            if re.search(r"[^\W\d_]", word) and re.search(r"[0-9-]", word):
                found.add(word.upper())                 # a code, whole: AX-300, E05, B-PROG
            elif len(word) > 1 and word.isalpha() and word.isupper():
                found.add(word)                         # a label printed in capitals: DRY, MODE, PIÙ
        pages.update(found)
    names = set()
    for (name,) in conn.execute("SELECT patient_name FROM patients"):
        names.update(w.upper() for w in re.findall(r"[^\W\d_]{3,}", name or ""))
    models = {d["model"].upper() for d in cg.devices(gconn)}
    terms = [t for t in pages if t not in names and not cg.CF_SHAPE.search(t)]
    return sorted(terms, key=lambda t: (t not in models, -pages[t], t))


def vocabulary(gconn, conn, role=GUIDE_ROLE):
    """The hint as sent to the device: the first MAX_TERMS of ranked_terms. The device fits them to its speech
    engine's prompt budget in the same order (jarvis/stt.py)."""
    return ranked_terms(gconn, conn, role)[:MAX_TERMS]


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
        out["escalation"] = "Say the wake phrase and ask again, naming the device."    # by voice there is no list
    if out.get("conflicting"):
        # each document that disagrees, named so the page can link it; nothing is chosen between them
        out["conflicting"] = [{**x, "title": (cg.source(gconn, x["source_id"]) or {"title": None})["title"]}
                              for x in out["conflicting"]]
    return out


def approved_guides(gconn, role=GUIDE_ROLE):
    """How many approved documents this device's questions can be answered from (the Jarvis page's readiness)."""
    audiences = ("staff", "dentist") if role == "dentist" else ("staff",)
    marks = ",".join("?" * len(audiences))
    return gconn.execute(f"SELECT COUNT(*) FROM sources WHERE status = 'approved' AND audience IN ({marks})",
                         audiences).fetchone()[0]


# --- notes by voice (J03, first slice; owner-authorised 2026-10-11) -----------------------------------------------
# The spoken name only proposes patients; the person at the device's screen picks one and saves. A draft lives here
# in memory for NOTE_SECONDS and is used once: it never survives a restart, and nothing is written until the save.

NOTE_SECONDS = 120
MAX_NOTE = 1000
MAX_MATCHES = 5
NOTE_PREFIX = "Dictated to Jarvis: "
_DRAFTS = {}
_DRAFTS_LOCK = __import__("threading").Lock()


def _fold(text):
    import unicodedata
    plain = "".join(c for c in unicodedata.normalize("NFKD", text or "") if not unicodedata.combining(c))
    return re.findall(r"[a-z0-9]+", plain.casefold())


SIMILAR = 0.8


def _sound(word):
    """A name as it sounds, so an English speech engine's spelling still finds the Italian one: Julia ~ Giulia,
    Kiara ~ Chiara, Rosi ~ Rossi."""
    w = re.sub(r"gi(?=[aeou])", "j", word)
    w = re.sub(r"gli", "li", w)
    w = w.replace("ch", "k").replace("gh", "g").replace("y", "i")
    return re.sub(r"(.)\1+", r"\1", w)


def _same_word(spoken, name_word):
    a, b = _sound(spoken), _sound(name_word)
    if a == b:
        return True
    import difflib
    return min(len(a), len(b)) >= 4 and difflib.SequenceMatcher(None, a, b).ratio() >= SIMILAR


def find_patients(conn, spoken):
    """Patients whose name has a word for every spoken word - the same word, or one that sounds the same or nearly
    (case and accents ignored). Only proposals: the doctor picks on screen. -> [{cf, name}]"""
    want = _fold(spoken)
    if not want:
        return []
    found = []
    for row in conn.execute("SELECT codice_fiscale, patient_name FROM patients WHERE codice_fiscale IS NOT NULL"
                            " ORDER BY patient_name, codice_fiscale"):
        have = _fold(row["patient_name"])
        if all(any(_same_word(w, h) for h in have) for w in want):
            found.append({"cf": row["codice_fiscale"], "name": row["patient_name"]})
    return found


def _delegated(conn, device, action, target, now, capability="append_note", what="add notes"):
    """The person the device acts for, allowed `capability` - or LinkError (audited, naming no patient)."""
    who = current_delegation(conn, device["id"], now)
    if who is None:
        log_audit(conn, f"jarvis-device:{device['id']}", "device", action, target, allowed=0, reason="no_delegation")
        raise LinkError("auth_required", "a staff member must hand their signed-in session to this device first")
    if who["role"] != DOCTOR or not authorize(who["role"], capability):
        log_audit(conn, who["username"], who["role"], action, target, allowed=0, reason="role")
        raise LinkError("role", f"only a doctor can let Jarvis {what}")
    return who


def draft_note(conn, device, spoken_patient, text, now=None):
    """-> {draft, choices: [{cf, name}], text, seconds}. Nothing is written."""
    now = _now(now)
    target = f"jarvis_device:{device['id']}"
    who = _delegated(conn, device, "jarvis_note_draft", target, now)
    text = " ".join((text or "").split())[:MAX_NOTE]
    if not text:
        raise LinkError("empty", "no note was heard")
    found = find_patients(conn, spoken_patient)
    log_audit(conn, who["username"], who["role"], "jarvis_note_draft", target, allowed=1,
              reason="no_match" if not found else "one_match" if len(found) == 1 else
              "too_many" if len(found) > MAX_MATCHES else "several_matches")
    if not found:
        raise LinkError("no_match", "no patient matches that name - say the patient's full name")
    if len(found) > MAX_MATCHES:
        raise LinkError("too_many", "too many patients match - say the patient's full name")
    draft = secrets.token_urlsafe(16)
    with _DRAFTS_LOCK:
        for key in [k for k, v in _DRAFTS.items() if v["expires"] <= now]:
            del _DRAFTS[key]
        _DRAFTS[draft] = {"device_id": device["id"], "username": who["username"], "text": text, "choices": found,
                          "expires": now + timedelta(seconds=NOTE_SECONDS)}
    return {"draft": draft, "choices": found, "text": text, "seconds": NOTE_SECONDS}


def commit_note(conn, device, draft, cf, save, now=None):
    """Save a draft's words on the chosen patient, through `save(note, role, username)` (the clinic's own save).
    The draft is used up whatever happens. -> {saved, cf, name, status}"""
    now = _now(now)
    with _DRAFTS_LOCK:
        d = _DRAFTS.pop(draft or "", None)
    target = cf or f"jarvis_device:{device['id']}"
    if d is None or d["device_id"] != device["id"] or now >= d["expires"]:
        log_audit(conn, f"jarvis-device:{device['id']}", "device", "jarvis_note", target, allowed=0, reason="draft_gone")
        raise LinkError("gone", "this note is no longer waiting - say it again")
    who = _delegated(conn, device, "jarvis_note", target, now)
    if who["username"] != d["username"]:
        log_audit(conn, who["username"], who["role"], "jarvis_note", target, allowed=0, reason="delegation_changed")
        raise LinkError("auth_required", "the person this device acts for has changed - say the note again")
    choice = next((c for c in d["choices"] if c["cf"] == cf), None)
    if choice is None:
        log_audit(conn, who["username"], who["role"], "jarvis_note", target, allowed=0, reason="not_proposed")
        raise LinkError("patient", "that patient was not one of the proposed ones")
    from dental_notes_schema import DentalNote
    note = DentalNote(patient_name=choice["name"], codice_fiscale=cf, visit_date=clinic_time.to_local(now).date(),
                      clinical_notes=NOTE_PREFIX + d["text"])
    status = save(note, who["role"], who["username"])
    log_audit(conn, who["username"], who["role"], "jarvis_note", cf, allowed=1, reason="jarvis_voice")
    return {"saved": status != "failed", "cf": cf, "name": choice["name"], "status": status}


# --- read and edit by voice, one sign-in (J03 slice 2; owner-authorised 2026-10-12) ----------------------------------
# Reading proposes patients as notes do; what is shown about them stays on the device's screen. A phone change is a
# draft until the doctor clicks a patient; it is then saved through the clinic's own pending action.

def _matches(conn, spoken, cf):
    """The patients a spoken name proposes, or the one patient Jarvis already handled (by codice fiscale)."""
    if cf:
        row = conn.execute("SELECT codice_fiscale, patient_name FROM patients WHERE codice_fiscale = ?", (cf,)).fetchone()
        return [{"cf": row["codice_fiscale"], "name": row["patient_name"]}] if row else []
    return find_patients(conn, spoken)


def _next_appointment(conn, cf):
    import appointments
    row = appointments.next_booked(conn, cf)
    if row is None:
        return None
    start = clinic_time.read_instant(row["starts_at"])
    return {"date": clinic_time.local_date(row["starts_at"]), "time": clinic_time.local_hhmm(row["starts_at"]),
            "weekday": clinic_time.to_local(start).strftime("%A"), "dentist": row["dentist"], "minutes": row["minutes"]}


def read_patients(conn, device, spoken_patient, want, cf=None, now=None):
    """`want` "open": the patients a spoken name matches, to open on screen; "next": the same, each with its next
    booked appointment. -> {choices: [{cf, name, next?}]}. Nothing is written but the audit."""
    now = _now(now)
    target = f"jarvis_device:{device['id']}"
    action = "jarvis_read"
    who = _delegated(conn, device, action, target, now, "read_notes", "read patient records")
    if want not in ("open", "next"):
        raise LinkError("empty", "nothing to read")
    found = _matches(conn, spoken_patient, cf)
    if not found:
        log_audit(conn, who["username"], who["role"], action, target, allowed=1, reason="no_match")
        raise LinkError("no_match", "no patient matches that name - say the patient's full name")
    if len(found) > MAX_MATCHES:
        log_audit(conn, who["username"], who["role"], action, target, allowed=1, reason="too_many")
        raise LinkError("too_many", "too many patients match - say the patient's full name")
    if want == "open":
        log_audit(conn, who["username"], who["role"], action, target, allowed=1,
                  reason="open_one" if len(found) == 1 else "open_several")
        return {"choices": found}
    for c in found:          # each patient whose appointment is shown, audited against that patient
        c["next"] = _next_appointment(conn, c["cf"])
        log_audit(conn, who["username"], who["role"], action, c["cf"], allowed=1, reason="next_appointment")
    return {"choices": found}


EDIT_FIELDS = ("phone",)


def draft_edit(conn, device, spoken_patient, field, value, cf=None, now=None):
    """A field change heard by the device -> {draft, field, value, choices: [{cf, name, before}], seconds}. Nothing is
    written. Only the phone, normalised as the clinic stores it."""
    import phones
    now = _now(now)
    target = f"jarvis_device:{device['id']}"
    who = _delegated(conn, device, "jarvis_edit_draft", target, now, "update_field", "change patient records")
    if field not in EDIT_FIELDS:
        raise LinkError("field", "Jarvis can change only a patient's phone number")
    value = phones.canonical(value or "")
    if value is None:
        log_audit(conn, who["username"], who["role"], "jarvis_edit_draft", target, allowed=1, reason="bad_value")
        raise LinkError("value", "that is not a valid Italian phone number")
    found = _matches(conn, spoken_patient, cf)
    log_audit(conn, who["username"], who["role"], "jarvis_edit_draft", target, allowed=1,
              reason="no_match" if not found else "one_match" if len(found) == 1 else
              "too_many" if len(found) > MAX_MATCHES else "several_matches")
    if not found:
        raise LinkError("no_match", "no patient matches that name - say the patient's full name")
    if len(found) > MAX_MATCHES:
        raise LinkError("too_many", "too many patients match - say the patient's full name")
    for c in found:
        c["before"] = conn.execute("SELECT phone FROM patients WHERE codice_fiscale = ?", (c["cf"],)).fetchone()[0]
    draft = secrets.token_urlsafe(16)
    with _DRAFTS_LOCK:
        for key in [k for k, v in _DRAFTS.items() if v["expires"] <= now]:
            del _DRAFTS[key]
        _DRAFTS[draft] = {"device_id": device["id"], "username": who["username"], "kind": "edit", "field": field,
                          "value": value, "choices": found, "expires": now + timedelta(seconds=NOTE_SECONDS)}
    return {"draft": draft, "field": field, "value": value, "choices": found, "seconds": NOTE_SECONDS}


def commit_edit(conn, device, draft, cf, apply, now=None):
    """Save a drafted change on the patient picked on screen, through `apply(pending, role, username)` (the clinic's
    own apply_pending_action). The draft is used up whatever happens. -> {saved, cf, name, field, value}"""
    import agent
    now = _now(now)
    with _DRAFTS_LOCK:
        d = _DRAFTS.pop(draft or "", None)
    target = cf or f"jarvis_device:{device['id']}"
    if d is None or d.get("kind") != "edit" or d["device_id"] != device["id"] or now >= d["expires"]:
        log_audit(conn, f"jarvis-device:{device['id']}", "device", "jarvis_edit", target, allowed=0, reason="draft_gone")
        raise LinkError("gone", "this change is no longer waiting - say it again")
    who = _delegated(conn, device, "jarvis_edit", target, now, "update_field", "change patient records")
    if who["username"] != d["username"]:
        log_audit(conn, who["username"], who["role"], "jarvis_edit", target, allowed=0, reason="delegation_changed")
        raise LinkError("auth_required", "the person this device acts for has changed - say the change again")
    choice = next((c for c in d["choices"] if c["cf"] == cf), None)
    if choice is None:
        log_audit(conn, who["username"], who["role"], "jarvis_edit", target, allowed=0, reason="not_proposed")
        raise LinkError("patient", "that patient was not one of the proposed ones")
    row = conn.execute("SELECT patient_name, phone FROM patients WHERE codice_fiscale = ?", (cf,)).fetchone()
    if row is None or row["phone"] != choice["before"] or row["patient_name"] != choice["name"]:
        log_audit(conn, who["username"], who["role"], "jarvis_edit", cf, allowed=0, reason="changed")
        raise LinkError("changed", "the record changed after the change was shown - say it again")
    call = agent.ToolCall(tool="update_field", args={"patient": row["patient_name"], "field": d["field"], "value": d["value"]})
    pending, reason = agent.build_pending_action(call, conn, who["role"], who["username"],
                                                 choose_cf=lambda candidates: cf if cf in candidates else None)
    if pending is None or pending["cf"] != cf:
        log_audit(conn, who["username"], who["role"], "jarvis_edit", cf, allowed=0, reason="not_built")
        raise LinkError("patient", reason or "the change could not be prepared")
    apply(pending, who["role"], who["username"])
    log_audit(conn, who["username"], who["role"], "jarvis_edit", cf, allowed=1, reason="jarvis_voice")
    return {"saved": True, "cf": cf, "name": row["patient_name"], "field": d["field"], "value": d["value"]}


# --- one sign-in: the Clinic tab signed in from Jarvis's sign-in (owner, 2026-10-11/12) -----------------------------

HANDOFF_SECONDS = 60
_TICKETS = {}


def handoff_ticket(conn, device, now=None):
    """A one-time link that signs a browser in as the doctor who signed this device in - until that sign-in ends.
    Kept in memory only, used once, HANDOFF_SECONDS. -> ticket"""
    now = _now(now)
    who = current_delegation(conn, device["id"], now)
    if who is None or who["kind"] != "login":
        log_audit(conn, f"jarvis-device:{device['id']}", "device", "jarvis_handoff", f"jarvis_device:{device['id']}",
                  allowed=0, reason="no_signin")
        raise LinkError("auth_required", "a doctor must sign Jarvis in first")
    row = conn.execute("SELECT id FROM jarvis_delegations WHERE device_id = ? AND revoked_at IS NULL ORDER BY id DESC"
                       " LIMIT 1", (device["id"],)).fetchone()
    ticket = secrets.token_urlsafe(32)
    with _DRAFTS_LOCK:
        for key in [k for k, v in _TICKETS.items() if v["expires"] <= now]:
            del _TICKETS[key]
        _TICKETS[web_session.token_hash(ticket)] = {"delegation_id": row["id"], "device_id": device["id"],
                                                     "username": who["username"],
                                                     "expires": now + timedelta(seconds=HANDOFF_SECONDS)}
    return ticket


def use_handoff(conn, ticket, now=None):
    """The hand-off link opened: -> (session token, username, role) for a new clinic session, recorded on the Jarvis
    sign-in so it ends with it; or LinkError. The ticket is gone whatever happens."""
    now = _now(now)
    with _DRAFTS_LOCK:
        t = _TICKETS.pop(web_session.token_hash(ticket or ""), None)
    if t is None or now >= t["expires"]:
        log_audit(conn, "jarvis-handoff", "unknown", "jarvis_handoff", "clinic", allowed=0, reason="ticket_gone")
        raise LinkError("gone", "this sign-in link is no longer valid")
    row = conn.execute("SELECT id FROM jarvis_delegations WHERE device_id = ? AND revoked_at IS NULL ORDER BY id DESC"
                       " LIMIT 1", (t["device_id"],)).fetchone()
    who = current_delegation(conn, t["device_id"], now)
    if row is None or row["id"] != t["delegation_id"] or who is None or who["username"] != t["username"] \
            or who["kind"] != "login":
        log_audit(conn, t["username"], "unknown", "jarvis_handoff", f"jarvis_device:{t['device_id']}", allowed=0,
                  reason="signin_ended")
        raise LinkError("gone", "the Jarvis sign-in this link came from has ended")
    token = web_session.create_session(conn, who["username"], who["role"], now)
    conn.execute("UPDATE jarvis_delegations SET session_hash = ? WHERE id = ?", (web_session.token_hash(token), row["id"]))
    conn.commit()
    log_audit(conn, who["username"], who["role"], "jarvis_handoff", f"jarvis_device:{t['device_id']}", allowed=1)
    return token, who["username"], who["role"]


def handoff_over(conn, token_hash_value, now=None):
    """True for a clinic session that came from a Jarvis sign-in which has since ended (00:00, sign out, Jarvis
    restarting, another doctor). A session not from a hand-off is never touched."""
    row = conn.execute("SELECT revoked_at, expires_at FROM jarvis_delegations WHERE kind = 'login' AND session_hash = ?"
                       " ORDER BY id DESC LIMIT 1", (token_hash_value,)).fetchone()
    if row is None:
        return False
    return row["revoked_at"] is not None or _now(now) >= clinic_time.read_instant(row["expires_at"])
