"""Delivery receipts (P11.04, P11.05). Accepted is not delivered.

A provider taking a message means it took the message. It does not mean anyone
received it (R07). P09 put `delivered` in the reminder schema and never set it,
because no channel gave evidence; this is the thing that would set it, from a
channel's own callback and from nothing else.

FOUR OUTCOMES, AND ONE OF THEM IS `unknown`.

    accepted   the provider took it. the most the sandbox can ever report.
    delivered  the channel says it arrived. only a receipt sets this.
    failed     the channel says it did not, and will not.
    unknown    we do not know, and we are not going to guess.

`unknown` is the point of this file. A timeout is not a failure and not a
success: the message may have gone. So it is recorded and left for
reconciliation, and nothing re-sends on its own - re-sending on a timeout is
how one reminder becomes three.

Receipts are idempotent per provider reference, and a receipt for a job that
was never handed to a provider is refused rather than invented.
"""
import sqlite3
import sys

import clinic_time
import providers

ACCEPTED, DELIVERED, FAILED, UNKNOWN = "accepted", "delivered", "failed", "unknown"
OUTCOMES = (ACCEPTED, DELIVERED, FAILED, UNKNOWN)

# a receipt may only move forward, and never out of a settled state. a provider
# that reports delivered and then, late and out of order, accepted must not
# walk the record backwards.
RANK = {ACCEPTED: 1, UNKNOWN: 1, FAILED: 2, DELIVERED: 3}

SCHEMA = """
    CREATE TABLE IF NOT EXISTS delivery_receipts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id INTEGER NOT NULL,
        provider_ref TEXT NOT NULL,
        outcome TEXT NOT NULL CHECK (outcome IN
            ('accepted', 'delivered', 'failed', 'unknown')),
        reason TEXT,
        at TEXT NOT NULL,
        UNIQUE (provider_ref, outcome)
    );
    CREATE INDEX IF NOT EXISTS idx_receipts_job ON delivery_receipts (job_id);
"""


class ReceiptRefused(Exception):
    pass


def record_accepted(conn, job_id, provider_ref, now=None):
    """The provider took it. This is the ceiling for a channel with no callback."""
    return _write(conn, job_id, provider_ref, ACCEPTED, now=now)


def record_unknown(conn, job_id, provider_ref, reason, now=None):
    """A timeout, or anything else that leaves the outcome genuinely open.

    NOT a retry. The caller must not re-send on the back of this.
    """
    return _write(conn, job_id, provider_ref, UNKNOWN, reason=reason, now=now)


def receipt(conn, provider_ref, outcome, reason=None, now=None):
    """A callback from the channel. The only thing that may say `delivered`."""
    if outcome not in OUTCOMES:
        raise ReceiptRefused("unknown outcome")
    row = conn.execute("SELECT job_id FROM delivery_receipts WHERE provider_ref = ?"
                       " ORDER BY id LIMIT 1", (provider_ref,)).fetchone()
    if row is None:
        # a receipt for something this clinic never sent. it is not evidence of
        # anything; inventing a job for it would be worse than dropping it.
        raise ReceiptRefused("no such provider reference")
    return _write(conn, row[0], provider_ref, outcome, reason=reason, now=now)


def _write(conn, job_id, provider_ref, outcome, reason=None, now=None):
    now = now or clinic_time.now_utc()
    cur = conn.execute(
        "INSERT OR IGNORE INTO delivery_receipts (job_id, provider_ref, outcome, reason, at)"
        " VALUES (?, ?, ?, ?, ?)",
        (job_id, provider_ref, outcome, reason, clinic_time.to_storage(now)))
    conn.commit()
    _apply(conn, job_id, now)
    return bool(cur.rowcount)


def state_of(conn, job_id):
    """The furthest-forward outcome seen for a job, or None."""
    rows = [r[0] for r in conn.execute(
        "SELECT outcome FROM delivery_receipts WHERE job_id = ?", (job_id,))]
    if not rows:
        return None
    return max(rows, key=lambda o: RANK[o])


def _apply(conn, job_id, now):
    """Reflect the receipt on the reminder job - forward only."""
    best = state_of(conn, job_id)
    if best == DELIVERED:
        conn.execute("UPDATE reminder_jobs SET status = 'delivered' WHERE id = ?"
                     " AND status IN ('sent', 'claimed')", (job_id,))
    elif best == FAILED:
        conn.execute("UPDATE reminder_jobs SET status = 'failed', last_error ="
                     " 'transport_rejected' WHERE id = ? AND status IN ('sent', 'claimed')",
                     (job_id,))
    conn.commit()


def unresolved(conn, older_than=None):
    """Jobs handed to a provider with no settled outcome. The reconcile list.

    These are the ones a person - or, later, a provider reconciliation API -
    has to look at. Nothing here retries them.
    """
    rows = conn.execute(
        "SELECT job_id, provider_ref, MAX(at) AS last_at FROM delivery_receipts"
        " GROUP BY job_id, provider_ref").fetchall()
    out = []
    for row in rows:
        if state_of(conn, row["job_id"]) in (DELIVERED, FAILED):
            continue
        if older_than and clinic_time.read_instant(row["last_at"]) > older_than:
            continue
        out.append(row)
    return out


def counts(conn):
    got = {o: 0 for o in OUTCOMES}
    for job_id, in conn.execute("SELECT DISTINCT job_id FROM delivery_receipts"):
        best = state_of(conn, job_id)
        if best:
            got[best] += 1
    return got


def selftest():
    import tempfile
    from pathlib import Path

    import patient_id
    import reminders
    from storage import init_db

    with tempfile.TemporaryDirectory() as tmp:
        conn = init_db(str(Path(tmp) / "clinic.sqlite"))
        t0 = clinic_time.read_instant("2026-09-22T08:00:00+00:00")
        pid = patient_id.seed_patient(conn, "ZZD00A00A000A", "Dario Consegna", "+39 055 1")
        job = conn.execute(
            "INSERT INTO reminder_jobs (kind, patient_id, subject_id, version, idempotency_key,"
            " send_at, status, created_at) VALUES ('appointment', ?, 1, 'v', 'k1', ?, 'sent', ?)",
            (pid, clinic_time.to_storage(t0), clinic_time.to_storage(t0))).lastrowid
        conn.commit()

        # 1. accepted is the ceiling without a callback. the job is NOT
        # delivered, and P09's rule still holds.
        assert record_accepted(conn, job, "ref-1", now=t0) is True
        assert state_of(conn, job) == ACCEPTED
        assert conn.execute("SELECT status FROM reminder_jobs WHERE id = ?",
                            (job,)).fetchone()[0] == "sent", \
            "1: ACCEPTED MUST NOT MARK A REMINDER DELIVERED"

        # 2. only a channel receipt says delivered
        assert receipt(conn, "ref-1", DELIVERED, now=t0) is True
        assert conn.execute("SELECT status FROM reminder_jobs WHERE id = ?",
                            (job,)).fetchone()[0] == "delivered"

        # 3. the same receipt twice changes nothing
        assert receipt(conn, "ref-1", DELIVERED, now=t0) is False, "3: idempotent"
        assert conn.execute("SELECT COUNT(*) FROM delivery_receipts WHERE provider_ref = 'ref-1'"
                            " AND outcome = 'delivered'").fetchone()[0] == 1

        # 4. out of order: a late `accepted` after `delivered` must not walk
        # the record backwards
        receipt(conn, "ref-1", ACCEPTED, now=t0)
        assert state_of(conn, job) == DELIVERED, "4: it went backwards"
        assert conn.execute("SELECT status FROM reminder_jobs WHERE id = ?",
                            (job,)).fetchone()[0] == "delivered"

        # 5. a receipt for something never sent is refused, not invented
        for bad in ("ref-never", "", "../../etc"):
            try:
                receipt(conn, bad, DELIVERED, now=t0)
                raise AssertionError(f"5: {bad!r} must be refused")
            except ReceiptRefused:
                pass
        try:
            receipt(conn, "ref-1", "made_up", now=t0)
            raise AssertionError("5: unknown outcome")
        except ReceiptRefused:
            pass

        # 6. UNKNOWN is a state, not a retry. it does not settle the job and
        # it leaves it on the reconcile list.
        job2 = conn.execute(
            "INSERT INTO reminder_jobs (kind, patient_id, subject_id, version, idempotency_key,"
            " send_at, status, created_at) VALUES ('invoice', ?, 2, 'v', 'k2', ?, 'sent', ?)",
            (pid, clinic_time.to_storage(t0), clinic_time.to_storage(t0))).lastrowid
        conn.commit()
        record_accepted(conn, job2, "ref-2", now=t0)
        record_unknown(conn, job2, "ref-2", "transport_timeout", now=t0)
        assert state_of(conn, job2) in (ACCEPTED, UNKNOWN)
        assert conn.execute("SELECT status FROM reminder_jobs WHERE id = ?",
                            (job2,)).fetchone()[0] == "sent", \
            "6: an unknown outcome must not settle the job"
        pending = unresolved(conn)
        assert [r["job_id"] for r in pending] == [job2], f"6: the reconcile list: {pending}"

        # 7. a failure from the channel settles it the other way
        receipt(conn, "ref-2", FAILED, reason="transport_rejected", now=t0)
        assert state_of(conn, job2) == FAILED
        assert conn.execute("SELECT status, last_error FROM reminder_jobs WHERE id = ?",
                            (job2,)).fetchone()[0] == "failed"
        assert unresolved(conn) == [], "7: settled, so off the list"
        assert counts(conn) == {ACCEPTED: 0, DELIVERED: 1, FAILED: 1, UNKNOWN: 0}

        # 8. no receipt carries provider prose - the reason is ours or nothing
        for (reason,) in conn.execute("SELECT reason FROM delivery_receipts WHERE reason IS NOT NULL"):
            assert reason in reminders.ERRORS, f"8: open-ended reason {reason!r}"
        conn.close()
    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    print("usage: python delivery.py --selftest")
    sys.exit(1)


if __name__ == "__main__":
    main()
