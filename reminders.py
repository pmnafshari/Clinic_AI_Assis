"""Reminder engine and send queue (P09). Channel-independent; nothing is sent.

There is no messaging provider (D01, P11), so the real transport is disabled:
due reminders wait in the queue as `scheduled`, and the staff screen says
sending is off and why. `TestTransport` exists for the selftests and is never
wired into an app. "sent" means handed to a transport; `delivered` is in the
schema for P11 and is never set here, because no channel gives delivery
evidence (R07).

A job is keyed kind:subject:version. The version is the thing the message is
about - an appointment's start, an installment's due date - so a reschedule
makes a new job and cancels the old one instead of editing it. Just before a
send the worker checks everything again: the subject still stands, it is not
paid, a phone is on file, the patient still consents to messaging.

Quiet hours are 20:00-09:00 clinic time and cut two ways. An appointment
reminder moves EARLIER, never later, so it cannot arrive after the appointment.
An invoice or installment notice moves FORWARD to the next permitted hour and
never onto an earlier day - it has nothing to be early for (owner decision
2026-09-22).

Lead times and wording are demo defaults. The owner has not approved the rest
of the reminder policy (HUMAN_PENDING).
"""
import sqlite3
import sys
from datetime import datetime, timedelta

import clinic_time
from auth import authorize, log_audit

APPOINTMENT_LEAD = timedelta(hours=24)
INSTALLMENT_DAYS_BEFORE = 3
INSTALLMENT_HOUR = 10
QUIET_START, QUIET_END, QUIET_FALLBACK = 20, 9, 19
CATCH_UP_MIN = timedelta(hours=2)
CLAIM_TIMEOUT = timedelta(minutes=10)
MAX_ATTEMPTS = 3
BACKOFF = timedelta(minutes=5)
BATCH = 50

TEMPLATES = {
    "appointment": {
        "it": "Promemoria dallo studio: appuntamento il {date} alle {time}. Per spostarlo chiami lo studio.",
        "en": "Reminder from the clinic: appointment on {date} at {time}. To change it, call the clinic.",
    },
    "invoice": {
        "it": "Dallo studio: un nuovo riepilogo di spesa è nel portale pazienti.",
        "en": "From the clinic: a new summary of charges is in the patient portal.",
    },
    "installment": {
        "it": "Promemoria dallo studio: una rata scade il {date}. I dettagli sono nel portale pazienti.",
        "en": "Reminder from the clinic: an installment is due on {date}. Details are in the patient portal.",
    },
}

SCHEMA = """
    CREATE TABLE IF NOT EXISTS reminder_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL CHECK (kind IN ('appointment', 'invoice', 'installment')),
        patient_id TEXT NOT NULL,
        subject_id INTEGER NOT NULL,
        version TEXT NOT NULL,
        idempotency_key TEXT NOT NULL UNIQUE,
        send_at TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN
            ('scheduled', 'claimed', 'sent', 'delivered', 'failed', 'cancelled')),
        attempts INTEGER NOT NULL DEFAULT 0,
        manual_retries INTEGER NOT NULL DEFAULT 0,
        next_attempt_at TEXT,
        claimed_by TEXT,
        claimed_at TEXT,
        last_error TEXT,
        cancel_reason TEXT,
        lang TEXT NOT NULL DEFAULT 'it',
        created_at TEXT NOT NULL,
        sent_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminder_jobs (status, send_at);
    CREATE INDEX IF NOT EXISTS idx_reminders_patient ON reminder_jobs (patient_id);

    CREATE TABLE IF NOT EXISTS reminder_job_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        status TEXT NOT NULL CHECK (status IN ('running', 'ok', 'failed')),
        transport TEXT,
        planned INTEGER,
        sent INTEGER,
        cancelled INTEGER,
        failed INTEGER
    );
"""

# closed vocabularies: nothing from a provider or a patient reaches these columns
CANCEL_REASONS = ("rescheduled", "appointment_cancelled", "paid", "invoice_void",
                  "consent_withdrawn", "no_phone", "too_late", "patient_gone", "demo_identity")
ERRORS = ("transport_unavailable", "transport_timeout", "transport_rejected")


class TransientError(Exception):
    """The transport did not take the message; try again later."""


class Timeout(TransientError):
    """The transport did not answer. It may or may not have sent: the retry
    reuses the idempotency key, and a provider that honours it will not send
    twice. Exactly-once on an external channel cannot be promised."""


class DisabledTransport:
    name = "disabled"
    # a fragment, so a caller can put it after its own "nothing is sent"
    reason = "no messaging provider is configured (D01, P11)"


class TestTransport:
    """Selftests only. Records what it would have sent."""
    name = "test"

    def __init__(self, fail=None):
        self.sent = []
        self.fail = list(fail or [])

    def send(self, phone, body, key):
        if self.fail:
            raise self.fail.pop(0)
        self.sent.append((phone, body, key))


def _stamp(instant):
    return clinic_time.to_storage(instant)


def in_quiet(instant):
    """True when a send at this instant would land inside quiet hours."""
    hour = clinic_time.to_local(instant).hour
    return hour >= QUIET_START or hour < QUIET_END


def outside_quiet(instant):
    """Move an APPOINTMENT reminder out of quiet hours - earlier, never later,
    so it cannot drift past the appointment it is about."""
    local = clinic_time.to_local(instant)
    if not in_quiet(instant):
        return instant
    day = local.date() if local.hour >= QUIET_START else local.date() - timedelta(days=1)
    return clinic_time.to_utc(datetime(day.year, day.month, day.day, QUIET_FALLBACK))


def next_permitted(instant):
    """Defer a MONEY reminder to the next moment the clinic may message.

    Forward only, and never onto an earlier day. A notice about an invoice or
    an installment has nothing to be early for, so quiet hours are a wait, not
    a shift backwards. Moving one earlier (what `outside_quiet` does, correctly,
    for appointments) put it in the previous evening, which is already past -
    it went out at once, inside the very hours it was meant to respect.
    Owner decision 2026-09-22.

    09:00 clinic time is the boundary and it neither vanishes nor repeats in
    Europe/Rome - the clocks move at 02:00 and 03:00 - so the deferred instant
    is always a single real moment, on both DST Sundays.
    """
    if not in_quiet(instant):
        return instant
    local = clinic_time.to_local(instant)
    day = local.date() if local.hour < QUIET_END else local.date() + timedelta(days=1)
    return clinic_time.to_utc(datetime(day.year, day.month, day.day, QUIET_END))


def _enqueue(conn, kind, pid, subject, version, send_at, now):
    key = f"{kind}:{subject}:{version}"
    cur = conn.execute(
        "INSERT OR IGNORE INTO reminder_jobs (kind, patient_id, subject_id, version,"
        " idempotency_key, send_at, status, created_at) VALUES (?, ?, ?, ?, ?, ?, 'scheduled', ?)",
        (kind, pid, subject, version, key, _stamp(send_at), _stamp(now)))
    return cur.rowcount


def _cancel(conn, job_id, reason):
    conn.execute("UPDATE reminder_jobs SET status = 'cancelled', cancel_reason = ?"
                 " WHERE id = ? AND status IN ('scheduled', 'claimed')", (reason, job_id))


def plan(conn, now=None):
    """Create the jobs that should exist and cancel the ones that no longer
    should. Safe to run any number of times."""
    now = now or clinic_time.now_utc()
    created = 0
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        for a in conn.execute(
                "SELECT id, patient_id, starts_at FROM appointments WHERE status = 'booked'"
                " AND starts_at > ? ORDER BY starts_at LIMIT 500", (_stamp(now),)).fetchall():
            start = clinic_time.read_instant(a["starts_at"])
            send_at = max(outside_quiet(start - APPOINTMENT_LEAD), now)
            created += _enqueue(conn, "appointment", a["patient_id"], a["id"], a["starts_at"],
                                send_at, now)
        # an appointment job whose version is no longer the booking's start
        for job in conn.execute(
                "SELECT j.id, a.status, a.starts_at, j.version FROM reminder_jobs j"
                " LEFT JOIN appointments a ON a.id = j.subject_id WHERE j.kind = 'appointment'"
                " AND j.status = 'scheduled'").fetchall():
            if job["status"] != "booked":
                _cancel(conn, job["id"], "appointment_cancelled")
            elif job["starts_at"] != job["version"]:
                _cancel(conn, job["id"], "rescheduled")
        for ev in conn.execute("SELECT * FROM billing_events WHERE consumed_at IS NULL"
                               " ORDER BY id LIMIT 500").fetchall():
            # money reminders wait for the next permitted hour; they are never
            # moved earlier, so a send_at can only ever be now or later
            if ev["event"] == "invoice_issued":
                created += _enqueue(conn, "invoice", ev["patient_id"], ev["invoice_id"],
                                    "issued", next_permitted(now), now)
            else:
                due = datetime.fromisoformat(ev["due_date"]) - timedelta(days=INSTALLMENT_DAYS_BEFORE)
                send_at = clinic_time.to_utc(due.replace(hour=INSTALLMENT_HOUR))
                created += _enqueue(conn, "installment", ev["patient_id"], ev["installment_id"],
                                    ev["due_date"], next_permitted(max(send_at, now)), now)
            conn.execute("UPDATE billing_events SET consumed_at = ? WHERE id = ?",
                         (_stamp(now), ev["id"]))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return created


def _still_valid(conn, job, now):
    """None if the message may go, else the closed reason it may not."""
    # P22: a seeded demo identity is never messaged - checked first, whatever transport is wired (fail closed)
    if conn.execute("SELECT 1 FROM demo_identities WHERE patient_id = ?", (job["patient_id"],)).fetchone():
        return "demo_identity"
    if conn.execute("SELECT 1 FROM patients WHERE patient_id = ?",
                    (job["patient_id"],)).fetchone() is None:
        return "patient_gone"
    if job["kind"] == "appointment":
        a = conn.execute("SELECT status, starts_at FROM appointments WHERE id = ?",
                         (job["subject_id"],)).fetchone()
        if a is None or a["status"] != "booked":
            return "appointment_cancelled"
        if a["starts_at"] != job["version"]:
            return "rescheduled"
        if clinic_time.read_instant(a["starts_at"]) - now < CATCH_UP_MIN:
            return "too_late"
    else:
        import ledger
        if job["kind"] == "invoice":
            s = ledger.summary(conn, job["subject_id"])
        else:
            inv = conn.execute("SELECT p.invoice_id FROM installments i JOIN installment_plans p"
                               " ON p.id = i.plan_id WHERE i.id = ?", (job["subject_id"],)).fetchone()
            s = ledger.summary(conn, inv[0]) if inv else None
        if s is None or s["state"] == "void":
            return "invoice_void"
        if job["kind"] == "invoice" and s["outstanding_cents"] <= 0:
            return "paid"
        if job["kind"] == "installment":
            open_left = [i for i in s["installments"] if i["id"] == job["subject_id"]]
            if not open_left or open_left[0]["open_cents"] <= 0:
                return "paid"
    import consent
    if not consent.allows(conn, job["patient_id"], "messaging"):
        return "consent_withdrawn"
    phone = conn.execute("SELECT phone FROM patients WHERE patient_id = ?",
                         (job["patient_id"],)).fetchone()[0]
    import phones
    if not phones.canonical(phone):
        return "no_phone"                 # none, or a stored value that is not a number: never dialled
    return None


def body(conn, job):
    """The message text, built at send time and never stored."""
    wording = TEMPLATES[job["kind"]][job["lang"]]
    if job["kind"] == "appointment":
        local = clinic_time.to_local(clinic_time.read_instant(job["version"]))
        return wording.format(date=local.strftime("%d/%m/%Y"), time=local.strftime("%H:%M"))
    if job["kind"] == "installment":
        d = job["version"]
        return wording.format(date=f"{d[8:10]}/{d[5:7]}/{d[:4]}")
    return wording


def release_stale(conn, now):
    """A claim older than CLAIM_TIMEOUT belongs to a worker that died."""
    cur = conn.execute("UPDATE reminder_jobs SET status = 'scheduled', claimed_by = NULL,"
                       " claimed_at = NULL WHERE status = 'claimed' AND claimed_at < ?",
                       (_stamp(now - CLAIM_TIMEOUT),))
    conn.commit()
    return cur.rowcount


def claim(conn, worker, now):
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM reminder_jobs WHERE status = 'scheduled' AND send_at <= ?"
            " AND (next_attempt_at IS NULL OR next_attempt_at <= ?) ORDER BY send_at LIMIT ?",
            (_stamp(now), _stamp(now), BATCH))]
        for job_id in ids:
            conn.execute("UPDATE reminder_jobs SET status = 'claimed', claimed_by = ?,"
                         " claimed_at = ? WHERE id = ?", (worker, _stamp(now), job_id))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return ids


def run_once(conn, transport, worker="worker", now=None):
    """One pass: recover stale claims, then claim and send what is due. With
    the disabled transport nothing is claimed - due jobs stay scheduled."""
    now = now or clinic_time.now_utc()
    report = {"transport": transport.name, "released": release_stale(conn, now),
              "sent": 0, "cancelled": 0, "retry": 0, "failed": 0}
    if isinstance(transport, DisabledTransport):
        report["reason"] = transport.reason
        report["due_waiting"] = conn.execute(
            "SELECT COUNT(*) FROM reminder_jobs WHERE status = 'scheduled' AND send_at <= ?",
            (_stamp(now),)).fetchone()[0]
        return report
    for job_id in claim(conn, worker, now):
        job = conn.execute("SELECT * FROM reminder_jobs WHERE id = ?", (job_id,)).fetchone()
        reason = _still_valid(conn, job, now)
        if reason:
            _cancel(conn, job_id, reason)
            conn.commit()
            report["cancelled"] += 1
            continue
        phone = conn.execute("SELECT phone FROM patients WHERE patient_id = ?",
                             (job["patient_id"],)).fetchone()[0]
        try:
            transport.send(phone, body(conn, job), job["idempotency_key"])
        except TransientError as e:
            attempts = job["attempts"] + 1
            error = "transport_timeout" if isinstance(e, Timeout) else "transport_unavailable"
            if attempts >= MAX_ATTEMPTS:
                conn.execute("UPDATE reminder_jobs SET status = 'failed', attempts = ?,"
                             " last_error = ? WHERE id = ?", (attempts, error, job_id))
                report["failed"] += 1
            else:
                conn.execute("UPDATE reminder_jobs SET status = 'scheduled', attempts = ?,"
                             " last_error = ?, next_attempt_at = ?, claimed_by = NULL,"
                             " claimed_at = NULL WHERE id = ?",
                             (attempts, error, _stamp(now + BACKOFF * (2 ** (attempts - 1))), job_id))
                report["retry"] += 1
            conn.commit()
            continue
        conn.execute("UPDATE reminder_jobs SET status = 'sent', attempts = attempts + 1,"
                     " sent_at = ?, last_error = NULL WHERE id = ?", (_stamp(now), job_id))
        conn.commit()
        report["sent"] += 1
    return report


def last_run(conn):
    return conn.execute("SELECT * FROM reminder_job_runs ORDER BY id DESC LIMIT 1").fetchone()


def run_job(conn, transport=None, now=None):
    """One pass of the whole engine: work out what should be queued, then send
    what is due. Recorded in reminder_job_runs so the screen can say when it
    last ran and against which transport."""
    transport = transport or DisabledTransport()
    now = now or clinic_time.now_utc()
    run_id = conn.execute("INSERT INTO reminder_job_runs (started_at, status, transport)"
                          " VALUES (?, 'running', ?)", (_stamp(now), transport.name)).lastrowid
    conn.commit()
    try:
        planned = plan(conn, now=now)
        report = run_once(conn, transport, now=now)
    except Exception:
        conn.execute("UPDATE reminder_job_runs SET status = 'failed', finished_at = ?"
                     " WHERE id = ?", (_stamp(clinic_time.now_utc()), run_id))
        conn.commit()
        raise
    report["planned"] = planned
    conn.execute("UPDATE reminder_job_runs SET status = 'ok', finished_at = ?, planned = ?,"
                 " sent = ?, cancelled = ?, failed = ? WHERE id = ?",
                 (_stamp(clinic_time.now_utc()), planned, report["sent"], report["cancelled"],
                  report["failed"], run_id))
    conn.commit()
    return report


def retry(conn, job_id, actor, role, now=None):
    """A person puts a failed job back in the queue. Checked again at send time."""
    if not authorize(role, "retry_reminder"):
        log_audit(conn, actor, role, "retry_reminder", f"reminder:{job_id}", allowed=0)
        raise PermissionError(f"{role} may not retry a reminder")
    now = now or clinic_time.now_utc()
    changed = conn.execute("UPDATE reminder_jobs SET status = 'scheduled', next_attempt_at = ?,"
                           " attempts = 0, manual_retries = manual_retries + 1, claimed_by = NULL,"
                           " claimed_at = NULL WHERE id = ? AND status = 'failed'",
                           (_stamp(now), job_id)).rowcount
    conn.commit()
    if changed:
        log_audit(conn, actor, role, "retry_reminder", f"reminder:{job_id}", allowed=1)
    return bool(changed)


def dashboard(conn):
    counts = {s: 0 for s in ("scheduled", "claimed", "sent", "delivered", "failed", "cancelled")}
    for row in conn.execute("SELECT status, COUNT(*) FROM reminder_jobs GROUP BY status"):
        counts[row[0]] = row[1]
    jobs = conn.execute(
        "SELECT j.*, p.patient_name FROM reminder_jobs j LEFT JOIN patients p"
        " ON p.patient_id = j.patient_id ORDER BY j.send_at DESC LIMIT 100").fetchall()
    return counts, jobs


def selftest():
    import tempfile
    import threading
    from pathlib import Path

    import consent
    import patient_id
    from storage import init_db

    def appt(conn, pid, local_text, minutes=30, status="booked"):
        """Appointment rows written straight in. Booking goes through the
        roster and opening hours (P05), and none of that is what P09 is about -
        the engine reads a patient, a start and a status."""
        stored = clinic_time.to_storage(clinic_time.to_utc(datetime.fromisoformat(local_text)))
        return conn.execute(
            "INSERT INTO appointments (patient_id, dentist, starts_at, minutes, status,"
            " created_at, updated_at) VALUES (?, 'drossi', ?, ?, ?, ?, ?)",
            (pid, stored, minutes, status, stored, stored)).lastrowid

    def jobs(conn, **where):
        sql = "SELECT * FROM reminder_jobs"
        if where:
            sql += " WHERE " + " AND ".join(f"{k} = ?" for k in where)
        return conn.execute(sql + " ORDER BY id", tuple(where.values())).fetchall()

    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "clinic.sqlite")
        conn = init_db(db)
        D, A = ("drossi", "dentist"), ("aassist", "assistant")
        # 10:00 clinic time: inside opening hours, so the quiet-hours rule is
        # not silently doing the work in checks that are not about it
        t0 = clinic_time.read_instant("2026-09-22T08:00:00+00:00")
        pid = patient_id.seed_patient(conn, "ZZR00A00A000A", "Rina Reminder", "+39 055 000001")
        consent.record(conn, pid, "messaging", True, *D)

        # 1. P09.T1 - the lead time, and quiet hours moving a send earlier
        day = appt(conn, pid, "2026-09-24T10:00")
        dawn = appt(conn, pid, "2026-09-24T08:00")
        assert plan(conn, now=t0) == 2, "1: one job per booked appointment"
        by_subject = {j["subject_id"]: j for j in jobs(conn)}
        assert clinic_time.to_local(clinic_time.read_instant(by_subject[day]["send_at"])) \
            == datetime(2026, 9, 23, 10, 0), "1: 24 h before, untouched outside quiet hours"
        assert clinic_time.to_local(clinic_time.read_instant(by_subject[dawn]["send_at"])) \
            == datetime(2026, 9, 22, 19, 0), "1: 08:00 is inside quiet hours - the evening before"
        assert outside_quiet(clinic_time.to_utc(datetime(2026, 9, 23, 21, 30))) \
            == clinic_time.to_utc(datetime(2026, 9, 23, 19, 0)), "1: 21:30 moves back to the same evening"
        for hour in (9, 12, 19):
            inside = clinic_time.to_utc(datetime(2026, 9, 23, hour))
            assert outside_quiet(inside) == inside, f"1: {hour}:00 is not quiet"

        # 1b. P09.T1b - a money reminder inside quiet hours WAITS. it moves
        # forward to the next 09:00 and never onto an earlier day (owner
        # decision 2026-09-22). every boundary of the 20:00-09:00 window, and
        # both Europe/Rome DST Sundays, where "the next 09:00" is a different
        # number of hours away than it looks
        def local(text):
            return clinic_time.to_utc(datetime.fromisoformat(text))

        for issued, expected in [
                ("2026-09-22T07:30", "2026-09-22T09:00"),   # early morning: same day
                ("2026-09-22T00:30", "2026-09-22T09:00"),   # just after midnight: same day
                ("2026-09-22T08:59:59", "2026-09-22T09:00"),  # the last quiet second
                ("2026-09-22T20:00", "2026-09-23T09:00"),   # quiet starts on the stroke
                ("2026-09-22T23:30", "2026-09-23T09:00"),   # late evening: the next day
                ("2026-09-22T09:00", "2026-09-22T09:00"),   # the first permitted second
                ("2026-09-22T19:59:59", "2026-09-22T19:59:59"),  # the last permitted second
                ("2026-09-22T12:00", "2026-09-22T12:00"),   # mid-afternoon is untouched
                # DST ends 2026-10-25 03:00 -> 02:00 (CEST +2 to CET +1)
                ("2026-10-24T23:30", "2026-10-25T09:00"),
                # DST starts 2027-03-28 02:00 -> 03:00 (CET +1 to CEST +2)
                ("2027-03-27T23:30", "2027-03-28T09:00"),
                ("2027-03-28T04:30", "2027-03-28T09:00")]:
            at = local(issued)
            got = next_permitted(at)
            assert got == local(expected), \
                f"1b: {issued} -> {clinic_time.to_local(got)}, expected {expected}"
            assert got >= at, f"1b: {issued} was moved backwards, to {clinic_time.to_local(got)}"
            assert not in_quiet(got), f"1b: {issued} landed back inside quiet hours"

        # the repeated hour itself cannot be written as a clinic-local time -
        # 02:30 happens twice on 2026-10-25 - so both real instants behind it
        # are given in UTC. both are quiet, and both wait for the same 09:00
        for utc_text in ("2026-10-25T00:30:00+00:00", "2026-10-25T01:30:00+00:00"):
            at = clinic_time.read_instant(utc_text)
            assert clinic_time.to_local(at).hour == 2, "1b: both passes read as 02:xx local"
            assert in_quiet(at) and next_permitted(at) == local("2026-10-25T09:00"), \
                f"1b: {utc_text} -> {clinic_time.to_local(next_permitted(at))}"

        # the two DST deferrals are the same wall time and a different number
        # of real hours - the proof the rule is applied in the zone, not in UTC
        assert local("2026-10-25T09:00") - local("2026-10-24T23:30") == timedelta(hours=10, minutes=30)
        assert local("2027-03-28T09:00") - local("2027-03-27T23:30") == timedelta(hours=8, minutes=30)

        # and the whole way through: an invoice issued at 07:30 is queued for
        # 09:00 that morning, not 19:00 the evening before
        quiet_now = local("2026-09-22T07:30")
        conn.execute("INSERT INTO billing_events (event, patient_id, invoice_id, created_at)"
                     " VALUES ('invoice_issued', ?, 77, ?)", (pid, _stamp(quiet_now)))
        conn.commit()
        assert plan(conn, now=quiet_now) == 1, "1b: the invoice event makes one job"
        inv = jobs(conn, kind="invoice")[0]
        assert clinic_time.read_instant(inv["send_at"]) == local("2026-09-22T09:00"), \
            f"1b: queued for {clinic_time.to_local(clinic_time.read_instant(inv['send_at']))}"
        assert clinic_time.read_instant(inv["send_at"]) > quiet_now, \
            "1b: THE REGRESSION - a notice queued in the past goes out at once, inside quiet hours"

        # an installment whose 10:00 has already passed waits for the next
        # permitted hour too, rather than going out the moment it is planned
        late = local("2026-09-22T22:00")
        conn.execute("INSERT INTO billing_events (event, patient_id, invoice_id, installment_id,"
                     " due_date, created_at) VALUES ('installment_due', ?, 78, 78, ?, ?)",
                     (pid, "2026-09-23", _stamp(late)))
        conn.commit()
        assert plan(conn, now=late) == 1
        ins = jobs(conn, kind="installment")[0]
        assert clinic_time.read_instant(ins["send_at"]) == local("2026-09-23T09:00"), \
            f"1b: installment queued for {clinic_time.to_local(clinic_time.read_instant(ins['send_at']))}"

        # an appointment keeps the opposite rule, deliberately: it may only
        # move earlier, or it would arrive after the thing it is about
        assert outside_quiet(local("2026-09-23T08:00")) == local("2026-09-22T19:00"), \
            "1b: the appointment rule is unchanged - earlier, never later"

        # those two have been checked; everything below is about appointments
        # and counts what is due, so take them out of the queue
        conn.execute("UPDATE reminder_jobs SET status = 'cancelled', cancel_reason = 'paid'"
                     " WHERE kind IN ('invoice', 'installment')")
        conn.commit()

        # 2. P09.T2 - running it again changes nothing; a reschedule is a new
        # job and the old one is cancelled, never edited into a new time
        assert plan(conn, now=t0) == 0 and len(jobs(conn, kind="appointment")) == 2, \
            "2: re-running plans nothing new"
        moved = clinic_time.to_storage(clinic_time.to_utc(datetime(2026, 9, 24, 15, 0)))
        conn.execute("UPDATE appointments SET starts_at = ? WHERE id = ?", (moved, day))
        conn.commit()
        assert plan(conn, now=t0) == 1, "2: the new time is a new job"
        old, new = [j for j in jobs(conn, subject_id=day)]
        assert old["status"] == "cancelled" and old["cancel_reason"] == "rescheduled", \
            f"2: the old job is cancelled, not edited: {tuple(old)[:9]}"
        assert new["status"] == "scheduled" and new["version"] == moved, "2: keyed on the new start"
        conn.execute("UPDATE appointments SET status = 'cancelled' WHERE id = ?", (dawn,))
        conn.commit()
        plan(conn, now=t0)
        assert jobs(conn, subject_id=dawn)[0]["cancel_reason"] == "appointment_cancelled", \
            "2: a cancelled appointment cancels its reminder"

        # 3. nothing is sent without a provider, and the queue says so
        due = appt(conn, pid, "2026-09-23T12:00")
        plan(conn, now=t0)
        t1 = clinic_time.read_instant("2026-09-22T10:30:00+00:00")
        off = run_once(conn, DisabledTransport(), now=t1)
        assert off["sent"] == 0 and off["due_waiting"] == 1 and "provider" in off["reason"], \
            f"3: the disabled transport claims nothing and says why: {off}"
        assert jobs(conn, subject_id=due)[0]["status"] == "scheduled", "3: it is still waiting"

        # 4. P09.T3 - a transient failure backs off, three attempts fail it,
        # and a person can put it back. the retry is checked again at send time
        flaky = TestTransport(fail=[TransientError(), Timeout(), TransientError()])
        for attempt in (1, 2, 3):
            run_once(conn, flaky, now=t1)
            job = jobs(conn, subject_id=due)[0]
            assert job["attempts"] == attempt, f"4: attempt {attempt}, got {job['attempts']}"
            if attempt < 3:
                assert job["status"] == "scheduled" and job["next_attempt_at"] > _stamp(t1), \
                    "4: it waits before trying again"
                conn.execute("UPDATE reminder_jobs SET next_attempt_at = ? WHERE id = ?",
                             (_stamp(t1), job["id"]))
                conn.commit()
        assert job["status"] == "failed" and job["last_error"] == "transport_unavailable", \
            f"4: three attempts is enough: {job['status']}/{job['last_error']}"
        try:
            retry(conn, job["id"], "anadmin", "admin")
            raise AssertionError("4: admin does not retry reminders")
        except PermissionError:
            pass
        assert retry(conn, job["id"], *A, now=t1), "4: reception may put it back"
        assert jobs(conn, subject_id=due)[0]["attempts"] == 0, "4: the count starts again"
        good = TestTransport()
        report = run_once(conn, good, now=t1)
        assert report["sent"] == 1 and len(good.sent) == 1, f"4: it goes on the retry: {report}"
        phone, text, key = good.sent[0]
        assert phone == "+39 055 000001" and key == f"appointment:{due}:{jobs(conn, subject_id=due)[0]['version']}"
        assert "Rina" not in text and "23/09/2026" in text and "12:00" in text, \
            f"4: a date and a time, no name: {text!r}"
        assert jobs(conn, subject_id=due)[0]["status"] == "sent", "4: sent, never delivered"
        assert not jobs(conn, status="delivered"), "4: delivered is never set - no channel reports it"

        # 5. a claim whose worker died is released, and two workers claiming at
        # once split the queue instead of both taking the same job
        # 12:15 the next day, so its reminder is already due at t1
        stale = appt(conn, pid, "2026-09-23T12:15")
        plan(conn, now=t0)
        stale_job = jobs(conn, subject_id=stale)[0]["id"]
        claim(conn, "worker-a", t1)
        assert jobs(conn, id=stale_job)[0]["status"] == "claimed"
        assert release_stale(conn, t1 + CLAIM_TIMEOUT + timedelta(minutes=1)) == 1, \
            "5: a claim older than the timeout goes back in the queue"
        assert jobs(conn, id=stale_job)[0]["status"] == "scheduled"
        # two workers going for the same queue at the same moment. a barrier
        # holds both at the door so the window is really open - without it the
        # first thread can finish before the second starts, and a claim that is
        # not atomic passes anyway (it did, until this was tightened)
        for minute in (20, 25, 30):
            appt(conn, pid, f"2026-09-23T12:{minute}")
        plan(conn, now=t0)
        expected = sorted(j["id"] for j in jobs(conn, status="scheduled")
                          if j["send_at"] <= _stamp(t1))
        assert len(expected) == 4, f"5: four due jobs to split, got {len(expected)}"
        taken, broke, lock, gate = [], [], threading.Lock(), threading.Barrier(2)

        def grab(name):
            own = sqlite3.connect(db, timeout=10)
            own.row_factory = sqlite3.Row
            gate.wait()
            try:
                got = claim(own, name, t1)
            except Exception as e:
                # a worker that loses the race waits its turn; it does not fall
                # over. this is the half of the claim a double-claim check
                # misses, because the loser crashes instead of claiming twice
                with lock:
                    broke.append(f"{name}: {type(e).__name__}: {e}")
                return
            finally:
                own.close()
            with lock:
                taken.extend(got)

        threads = [threading.Thread(target=grab, args=(f"worker-{n}",)) for n in "bc"]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not broke, f"5: a worker fell over instead of waiting its turn: {broke}"
        assert sorted(taken) == expected, \
            f"5: every due job claimed exactly once between two workers: {sorted(taken)}"
        conn.execute("UPDATE reminder_jobs SET status = 'scheduled', claimed_at = NULL,"
                     " claimed_by = NULL WHERE status = 'claimed'")
        conn.execute("UPDATE reminder_jobs SET status = 'cancelled', cancel_reason = 'too_late'"
                     " WHERE status = 'scheduled' AND id != ?", (stale_job,))
        conn.commit()

        # 6. P09.T-consent - every send-time re-check, each cancelling with a
        # reason from the closed list and nothing reaching the transport
        def refuses(reason, prepare, undo):
            prepare()
            plan(conn, now=t0)
            probe = TestTransport()
            run_once(conn, probe, now=t1)
            got = jobs(conn, id=stale_job)[0]
            assert got["status"] == "cancelled" and got["cancel_reason"] == reason, \
                f"6: expected {reason}, got {got['status']}/{got['cancel_reason']}"
            assert not probe.sent, f"6: {reason} reached the transport"
            assert reason in CANCEL_REASONS
            undo()
            conn.execute("UPDATE reminder_jobs SET status = 'scheduled', cancel_reason = NULL"
                         " WHERE id = ?", (stale_job,))
            conn.commit()

        refuses("consent_withdrawn",
                lambda: consent.record(conn, pid, "messaging", False, *D),
                lambda: consent.record(conn, pid, "messaging", True, *D))
        refuses("no_phone",
                lambda: conn.execute("UPDATE patients SET phone = NULL WHERE patient_id = ?", (pid,)),
                lambda: conn.execute("UPDATE patients SET phone = '+39 055 000001'"
                                     " WHERE patient_id = ?", (pid,)))
        # closer than the catch-up window: a reminder that would land after the
        # thing it reminds about is cancelled, not sent
        soon = _stamp(t1 + CATCH_UP_MIN - timedelta(minutes=5))

        def bring_forward():
            conn.execute("UPDATE appointments SET starts_at = ? WHERE id = ?", (soon, stale))
            conn.execute("UPDATE reminder_jobs SET version = ? WHERE id = ?", (soon, stale_job))
            conn.commit()

        refuses("too_late", bring_forward, lambda: None)
        print("selftest ok")
        conn.close()


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    print("usage: python reminders.py --selftest")
    sys.exit(1)


if __name__ == "__main__":
    main()
