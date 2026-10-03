"""Booking and completion for demo imaging requests (P27).

SYNTHETIC DEMO. A booking is an ordinary clinic appointment linked to one version of an active demo request; a
completion is the dentist's record that a confirmed image of this patient, linked to this request, belongs to it.
Nothing is sent, ordered, billed or read clinically.

RECEPTION BOOKS ONLY WHAT THE DENTIST RECORDED. A booking starts from an active request of the patient whose record
is open, in one transaction with the appointment itself, so a stale page, a cancelled or replaced request, a second
click or a second receptionist books nothing twice.

NOTHING DISAPPEARS OR MOVES BY ITSELF (P27-D1). When the dentist cancels or revises the request, or the appointment
is cancelled or moved anywhere else (portal, Appointments page, chat), the appointment is left as it is and the
booking is flagged as needing reception action. It stays listed until someone records a resolution: who, when,
which and why. No patient is contacted and no booking is re-attached to a new version without that step.

THE PORTAL AND REMINDERS SEE AN APPOINTMENT. The link lives here, never in appointments.note.
"""
import sys

import appointments
import clinic_time
import imaging_requests as ir
from auth import authorize, log_audit

BOOK = "manage_appointments"
FLAGS = {"request_cancelled": "the dentist cancelled this request",
         "request_revised": "the dentist replaced this request with a new version",
         "appointment_cancelled": "the appointment was cancelled",
         "appointment_moved": "the appointment was moved outside this booking"}
# what reception may record for each flag
RESOLUTIONS = {"request_cancelled": ("appointment_cancelled", "kept_unlinked"),
               "request_revised": ("reattached", "appointment_cancelled", "kept_unlinked"),
               "appointment_cancelled": ("closed",),
               "appointment_moved": ("accepted_new_time", "appointment_cancelled", "kept_unlinked")}
RESOLUTION_LABELS = {"reattached": "Keep the appointment for the current version",
                     "accepted_new_time": "Keep the new time",
                     "appointment_cancelled": "Cancel the appointment",
                     "kept_unlinked": "Keep the appointment, not for imaging",
                     "closed": "Close: nothing booked for now"}

SCHEMA = """
    CREATE TABLE IF NOT EXISTS imaging_bookings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        series_id INTEGER NOT NULL, request_id INTEGER NOT NULL, request_version INTEGER NOT NULL,
        appointment_id INTEGER NOT NULL, patient_id TEXT NOT NULL,
        booked_by TEXT NOT NULL, booked_at TEXT NOT NULL,
        appt_starts_at TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('booked', 'needs_action', 'resolved')),
        flag TEXT CHECK (flag IS NULL OR flag IN ('request_cancelled', 'request_revised', 'appointment_cancelled',
                                                   'appointment_moved')),
        flagged_at TEXT,
        resolution TEXT CHECK (resolution IS NULL OR resolution IN ('reattached', 'accepted_new_time',
                               'appointment_cancelled', 'kept_unlinked', 'closed')),
        resolution_note TEXT, resolved_by TEXT, resolved_at TEXT,
        submit_token TEXT NOT NULL UNIQUE
    );
    -- one live booking per request series and per appointment
    CREATE UNIQUE INDEX IF NOT EXISTS idx_imaging_bookings_series ON imaging_bookings (series_id)
        WHERE state != 'resolved';
    CREATE UNIQUE INDEX IF NOT EXISTS idx_imaging_bookings_appt ON imaging_bookings (appointment_id)
        WHERE state != 'resolved';
    CREATE INDEX IF NOT EXISTS idx_imaging_bookings_patient ON imaging_bookings (patient_id, state);
    CREATE TABLE IF NOT EXISTS imaging_completions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        series_id INTEGER NOT NULL, request_id INTEGER NOT NULL, patient_id TEXT NOT NULL,
        document_id INTEGER NOT NULL,
        recorded_by TEXT NOT NULL, recorded_at TEXT NOT NULL, verified INTEGER NOT NULL CHECK (verified = 1),
        reversed_by TEXT, reversed_at TEXT, reversal_reason TEXT
    );
    CREATE UNIQUE INDEX IF NOT EXISTS idx_imaging_completions_live ON imaging_completions (series_id)
        WHERE reversed_at IS NULL;
"""


class BookingError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _now(now=None):
    return clinic_time.to_storage(now or clinic_time.now_utc())


def _require(conn, actor, role, caps, action, target):
    for cap in caps:
        if not authorize(role, cap):
            log_audit(conn, actor, role, action, target, allowed=0)
            raise PermissionError(f"{role} may not {action.replace('_', ' ')}")


def get(conn, booking_id):
    return conn.execute("SELECT * FROM imaging_bookings WHERE id = ?", (booking_id,)).fetchone()


def _mine(conn, booking_id, pid):
    b = conn.execute("SELECT * FROM imaging_bookings WHERE id = ? AND patient_id = ?", (booking_id, pid)).fetchone()
    if b is None:
        raise LookupError("no such booking for this patient")
    return b


def live_for_series(conn, series_id):
    return conn.execute("SELECT * FROM imaging_bookings WHERE series_id = ? AND state != 'resolved'",
                        (series_id,)).fetchone()


def live_for_request(conn, request_id):
    return conn.execute("SELECT * FROM imaging_bookings WHERE request_id = ? AND state != 'resolved'",
                        (request_id,)).fetchone()


def completion_for(conn, request_id):
    r = ir.get(conn, request_id)
    if r is None:
        return None
    return conn.execute("SELECT * FROM imaging_completions WHERE series_id = ? AND reversed_at IS NULL",
                        (r["series_id"],)).fetchone()


def _flag(conn, b, flag, now=None):
    conn.execute("UPDATE imaging_bookings SET state = 'needs_action', flag = ?, flagged_at = ? WHERE id = ?",
                 (flag, _now(now), b["id"]))
    r = ir.get(conn, b["request_id"])
    if r is not None:
        ir._event(conn, r, "booking_flagged", "system", "system", now)


# --- hooks: called inside other modules' transactions ----------------------------------------------------

def reconcile(conn, appointment_id, now=None):
    """The appointment behind a live booking was cancelled or moved: flag it. Our own changes update first."""
    b = conn.execute("SELECT * FROM imaging_bookings WHERE appointment_id = ? AND state = 'booked'",
                     (appointment_id,)).fetchone()
    if b is None:
        return
    a = conn.execute("SELECT status, starts_at FROM appointments WHERE id = ?", (appointment_id,)).fetchone()
    if a is None or a["status"] != appointments.BOOKED:
        _flag(conn, b, "appointment_cancelled", now)
    elif a["starts_at"] != b["appt_starts_at"]:
        _flag(conn, b, "appointment_moved", now)


def reconcile_all(conn):
    """Catch a change made by any path that did not call reconcile (a direct write). Read-time safety net."""
    rows = conn.execute("SELECT b.appointment_id FROM imaging_bookings b LEFT JOIN appointments a"
                        " ON a.id = b.appointment_id WHERE b.state = 'booked' AND (a.id IS NULL OR a.status != ?"
                        " OR a.starts_at != b.appt_starts_at)", (appointments.BOOKED,)).fetchall()
    if rows:
        ir._write(conn, lambda: [reconcile(conn, r[0]) for r in rows])


def on_request_ended(conn, r, flag, now=None):
    """The dentist cancelled or replaced request r: its live booking needs reception action, appointment untouched."""
    b = live_for_request(conn, r["id"])
    if b is not None:
        _flag(conn, b, flag, now)


def refuse_if_completed(conn, r):
    if conn.execute("SELECT 1 FROM imaging_completions WHERE series_id = ? AND reversed_at IS NULL",
                    (r["series_id"],)).fetchone():
        raise ir.RequestError("completed", "this request is recorded as done: reverse the completion first")


# --- booking ---------------------------------------------------------------------------------------------

def book(conn, request_id, pid, expected_version, dentist, starts_at, minutes, actor, role, token, now=None):
    """Book an ordinary appointment for this active request. The same one-time token twice is one booking."""
    _require(conn, actor, role, (ir.VIEW, BOOK), "imaging_book", f"imaging:{request_id}")
    if not token:
        raise BookingError("token", "the form has expired: open the request again")

    def do():
        seen = conn.execute("SELECT id, patient_id, request_id FROM imaging_bookings WHERE submit_token = ?",
                            (token,)).fetchone()
        if seen:
            if seen["patient_id"] != pid or seen["request_id"] != request_id:
                raise BookingError("token", "the form has expired: open the request again")
            return seen["id"], True
        r = ir._mine(conn, request_id, pid)
        if r["state"] != "active":
            raise BookingError("not_active", "this request is not active: nothing can be booked for it")
        if r["version"] != int(expected_version):
            raise BookingError("stale", "the request changed since this page was opened: reload it")
        if live_for_series(conn, r["series_id"]) is not None:
            raise BookingError("already_booked", "this request already has an appointment: open it instead")
        appt = appointments.book(conn, pid, dentist, starts_at, minutes, commit=False)
        stored = conn.execute("SELECT starts_at FROM appointments WHERE id = ?", (appt,)).fetchone()[0]
        cur = conn.execute("INSERT INTO imaging_bookings (series_id, request_id, request_version, appointment_id,"
                           " patient_id, booked_by, booked_at, appt_starts_at, state, submit_token)"
                           " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'booked', ?)",
                           (r["series_id"], r["id"], r["version"], appt, pid, actor, _now(now), stored, token))
        ir._event(conn, r, "booked", actor, role, now)
        return cur.lastrowid, False
    bid, repeated = ir._write(conn, do)
    if not repeated:
        log_audit(conn, actor, role, "imaging_book", f"imaging:{request_id}", allowed=1, reason=f"booking:{bid}")
    return bid


def move(conn, booking_id, pid, starts_at, minutes, actor, role, now=None):
    """Move the appointment of a live booking; the booking follows because it is our own change."""
    _require(conn, actor, role, (ir.VIEW, BOOK), "imaging_book_move", f"booking:{booking_id}")

    def do():
        b = _mine(conn, booking_id, pid)
        if b["state"] != "booked":
            raise BookingError("not_booked", "this booking needs reception action first")
        stored = appointments._window(starts_at, appointments._check_minutes(minutes))[1]
        conn.execute("UPDATE imaging_bookings SET appt_starts_at = ? WHERE id = ?", (stored, booking_id))
        appointments.reschedule(conn, b["appointment_id"], starts_at, minutes, commit=False)
        ir._event(conn, ir.get(conn, b["request_id"]), "moved", actor, role, now)
        return b["request_id"]
    rid = ir._write(conn, do)
    log_audit(conn, actor, role, "imaging_book_move", f"imaging:{rid}", allowed=1, reason=f"booking:{booking_id}")


def resolve(conn, booking_id, pid, resolution, note, actor, role, now=None):
    """Reception's recorded answer to a flagged booking. Nothing leaves the list without one."""
    _require(conn, actor, role, (ir.VIEW, BOOK), "imaging_book_resolve", f"booking:{booking_id}")
    note = " ".join((note or "").split())[:200]

    def do():
        reconcile(conn, _mine(conn, booking_id, pid)["appointment_id"], now)
        b = _mine(conn, booking_id, pid)
        if b["state"] != "needs_action":
            raise BookingError("not_flagged", "this booking does not need action: reload it")
        if resolution not in RESOLUTIONS[b["flag"]]:
            raise BookingError("resolution", "that is not a way to resolve this task")
        if resolution != "accepted_new_time" and not note:
            raise BookingError("note_needed", "write why, in a few words")
        a = conn.execute("SELECT status, starts_at FROM appointments WHERE id = ?", (b["appointment_id"],)).fetchone()
        booked = a is not None and a["status"] == appointments.BOOKED
        if resolution == "accepted_new_time":
            if not booked:
                raise BookingError("resolution", "the appointment is not booked any more")
            conn.execute("UPDATE imaging_bookings SET state = 'booked', flag = NULL, flagged_at = NULL,"
                         " appt_starts_at = ? WHERE id = ?", (a["starts_at"], booking_id))
            ir._event(conn, ir.get(conn, b["request_id"]), "booking_resolved", actor, role, now)
            return b["request_id"]
        conn.execute("UPDATE imaging_bookings SET state = 'resolved', resolution = ?, resolution_note = ?,"
                     " resolved_by = ?, resolved_at = ? WHERE id = ?",
                     (resolution, note, actor, _now(now), booking_id))
        ir._event(conn, ir.get(conn, b["request_id"]), "booking_resolved", actor, role, now)
        if resolution == "appointment_cancelled" and booked:
            appointments.cancel(conn, b["appointment_id"], commit=False)
        if resolution == "reattached":
            current = conn.execute("SELECT * FROM imaging_requests WHERE series_id = ? AND state = 'active'",
                                   (b["series_id"],)).fetchone()
            if current is None or not booked:
                raise BookingError("resolution", "there is no active version, or no booked appointment, to keep")
            conn.execute("INSERT INTO imaging_bookings (series_id, request_id, request_version, appointment_id,"
                         " patient_id, booked_by, booked_at, appt_starts_at, state, submit_token)"
                         " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'booked', ?)",
                         (b["series_id"], current["id"], current["version"], b["appointment_id"], pid, actor,
                          _now(now), a["starts_at"], f"reattach-{booking_id}"))
            ir._event(conn, current, "booked", actor, role, now)
        return b["request_id"]
    rid = ir._write(conn, do)
    log_audit(conn, actor, role, "imaging_book_resolve", f"imaging:{rid}", allowed=1,
              reason=f"booking:{booking_id} {resolution}")


# --- completion (dentist only) ---------------------------------------------------------------------------

def complete(conn, request_id, pid, expected_version, document_id, verified, actor, role, now=None):
    """The dentist records the request done with one confirmed image of this patient already linked to it."""
    _require(conn, actor, role, (ir.MANAGE,), "imaging_complete", f"imaging:{request_id}")
    if not verified:
        raise BookingError("not_verified", "tick that this image belongs to this patient and this request")

    def do():
        r = ir._mine(conn, request_id, pid)
        if r["state"] != "active":
            raise BookingError("not_active", "only an active request can be recorded as done")
        if r["version"] != int(expected_version):
            raise BookingError("stale", "the request changed since this page was opened: reload it")
        if completion_for(conn, request_id) is not None:
            raise BookingError("already_done", "this request is already recorded as done")
        linked = conn.execute("SELECT 1 FROM imaging_request_files f JOIN patient_documents d ON d.id = f.document_id"
                              " AND d.patient_id = f.patient_id WHERE f.request_id = ? AND f.document_id = ? AND"
                              " f.patient_id = ? AND d.status = 'confirmed'", (request_id, document_id, pid)).fetchone()
        if linked is None:
            raise BookingError("not_linked", "choose a confirmed image of this patient linked to this request")
        conn.execute("INSERT INTO imaging_completions (series_id, request_id, patient_id, document_id, recorded_by,"
                     " recorded_at, verified) VALUES (?, ?, ?, ?, ?, ?, 1)",
                     (r["series_id"], request_id, pid, document_id, actor, _now(now)))
        ir._event(conn, r, "completed", actor, role, now)
    ir._write(conn, do)
    log_audit(conn, actor, role, "imaging_complete", f"imaging:{request_id}", allowed=1,
              reason=f"document:{document_id}")


def reverse(conn, request_id, pid, reason, actor, role, now=None):
    _require(conn, actor, role, (ir.MANAGE,), "imaging_complete_reverse", f"imaging:{request_id}")
    reason = " ".join((reason or "").split())[:200]
    if not reason:
        raise BookingError("note_needed", "write why the completion is reversed")

    def do():
        r = ir._mine(conn, request_id, pid)
        c = completion_for(conn, request_id)
        if c is None:
            raise BookingError("not_done", "this request is not recorded as done")
        conn.execute("UPDATE imaging_completions SET reversed_by = ?, reversed_at = ?, reversal_reason = ? WHERE id = ?",
                     (actor, _now(now), reason, c["id"]))
        ir._event(conn, r, "completion_reversed", actor, role, now)
    ir._write(conn, do)
    log_audit(conn, actor, role, "imaging_complete_reverse", f"imaging:{request_id}", allowed=1)


# --- reading ---------------------------------------------------------------------------------------------

def _when(stored):
    return clinic_time.to_local(clinic_time.read_instant(stored)).strftime("%d/%m/%Y %H:%M")


def status_line(conn, r):
    """One line for the record card and the handoff: booked, needs action, done - never the file or the note."""
    c = conn.execute("SELECT * FROM imaging_completions WHERE series_id = ? AND reversed_at IS NULL",
                     (r["series_id"],)).fetchone()
    if c is not None and c["request_id"] == r["id"]:
        return f"Done - recorded by {c['recorded_by']} on {_when(c['recorded_at'])}."
    b = live_for_request(conn, r["id"])
    if b is None:
        return "Not booked." if r["state"] == "active" else ""
    if b["state"] == "needs_action":
        return f"Appointment {_when(b['appt_starts_at'])} needs reception action: {FLAGS[b['flag']]}."
    return f"Booked for {_when(b['appt_starts_at'])}."


def booking_view(conn, r):
    """The live booking of this request version, or None, with its appointment's time and dentist."""
    b = live_for_request(conn, r["id"])
    if b is None:
        return None
    a = conn.execute("SELECT dentist, status, minutes FROM appointments WHERE id = ?", (b["appointment_id"],)).fetchone()
    out = dict(b)
    out["when"] = _when(b["appt_starts_at"])
    out["dentist"] = a["dentist"] if a else ""
    out["minutes"] = a["minutes"] if a else 0
    out["reason"] = FLAGS.get(b["flag"] or "", "")
    out["choices"] = [(k, RESOLUTION_LABELS[k]) for k in RESOLUTIONS.get(b["flag"] or "", ())]
    return out


def tasks(conn, actor, role):
    """Every booking that needs reception action, oldest first. Resolved ones leave the list, and only then."""
    _require(conn, actor, role, (ir.VIEW,), "imaging_read", "imaging_tasks")
    reconcile_all(conn)
    rows = conn.execute("SELECT b.*, p.patient_name, p.codice_fiscale FROM imaging_bookings b"
                        " JOIN patients p ON p.patient_id = b.patient_id WHERE b.state = 'needs_action'"
                        " ORDER BY b.flagged_at, b.id").fetchall()
    out = []
    for b in rows:
        d = dict(b)
        d["when"] = _when(b["appt_starts_at"])
        d["reason"] = FLAGS[b["flag"]]
        out.append(d)
    return out


def patient_tasks(conn, pid):
    """This patient's bookings that need reception action, whatever state their request is in."""
    reconcile_all(conn)
    out = []
    for b in conn.execute("SELECT * FROM imaging_bookings WHERE patient_id = ? AND state = 'needs_action'"
                          " ORDER BY flagged_at, id", (pid,)):
        d = dict(b)
        d["when"] = _when(b["appt_starts_at"])
        d["reason"] = FLAGS[b["flag"]]
        out.append(d)
    return out


def task_count(conn):
    return conn.execute("SELECT COUNT(*) FROM imaging_bookings WHERE state = 'needs_action'").fetchone()[0]


def on_erase(conn, pid):
    """Inside the erasure transaction."""
    for table in ("imaging_completions", "imaging_bookings"):
        conn.execute(f"DELETE FROM {table} WHERE patient_id = ?", (pid,))


if __name__ == "__main__":
    if sys.argv[1:] == ["--selftest"]:
        import imaging_bookings_selftest
        imaging_bookings_selftest.selftest()
