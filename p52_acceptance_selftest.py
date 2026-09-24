"""Phase 52 acceptance: one appointment, one time, on every surface.

WHAT THE UNIT TESTS CANNOT SEE. clinic_time proves the conversion, appointments
proves the read paths and availability proves the schedule rule - each against
its own fixtures. None of them can catch the failure this phase is actually
about: two surfaces reading the SAME stored row and rendering two different
times, because one of them converts and the other slices. That is invisible to
any test that only looks at one screen.

So every check here books through the real write path and then asks two or more
surfaces what time it is. They must agree, and they must agree with what the
receptionist typed.

CEST DATES ON PURPOSE. Summer is +2 in Europe/Rome, so a surface that forgot to
convert is two hours out and fails loudly. A winter-only fixture would pass at
+1 for a path that dropped the offset entirely, and a UTC-only fixture would
pass for a path that never converted at all.
"""

import sys
import tempfile
from datetime import datetime
from pathlib import Path

import appointments
import availability
import clinic_time
import demo_fixtures
import patient_id
import storage

# Both are Wednesdays, both are in the future so the patient portal (which
# shows today onward) has something to render, and they sit either side of the
# October DST change so the two offsets are genuinely different.
SUMMER = "2026-09-23"        # CEST (+2)
WINTER = "2026-11-18"        # CET  (+1)

# "in the future" is only true against a fixed clinic clock. Unpinned, the test
# started failing on 2026-09-24 because SUMMER had become yesterday and the portal
# (rightly) no longer showed it. Every read here goes through clinic_time.now().
TODAY = datetime(2026, 9, 17, 12, 0)


def _clinic(tmp):
    conn = storage.init_db(str(Path(tmp) / "p52.sqlite"))
    demo_fixtures.seed(conn)
    return conn


def _agenda_times(conn, day):
    """What the STAFF screen shows, through the real view builder."""
    import app.dashboard_routes as dashboard_routes
    rows = appointments.agenda(conn, day)
    return [r["time"] for r in dashboard_routes._agenda_view(rows, clinic_time.now())]


def _portal_times(conn, patient):
    """What the PATIENT screen shows, through the real template filter."""
    from patient_app import create_patient_app
    app = create_patient_app()
    when = app.jinja_env.filters["appt_when"]
    booked, _ = appointments.open_for_patient(conn, patient)
    return [when(r["starts_at"], "en") for r in booked]


def selftest():
    with tempfile.TemporaryDirectory() as tmp:
        conn = _clinic(tmp)
        alice = patient_id.seed_patient(conn, "RSSM800010150100", "Alice Rossi")
        bob = patient_id.seed_patient(conn, "BNCG850020260200", "Bob Bianchi")
        conn.commit()

        # 1. ONE APPOINTMENT, TWO SURFACES, ONE TIME. booked at 10:30 in the
        # summer, when the clinic is +2 from UTC. the stored value is 08:30Z and
        # neither screen may ever show that.
        appointments.book(conn, alice, "dentist", f"{SUMMER}T10:30", 30)
        stored = conn.execute("SELECT starts_at FROM appointments").fetchone()["starts_at"]
        assert stored == f"{SUMMER}T08:30:00+00:00", f"1: stored as a UTC instant, got {stored}"
        assert _agenda_times(conn, SUMMER) == ["10:30"], \
            f"1: the staff agenda shows clinic time, got {_agenda_times(conn, SUMMER)}"
        portal = _portal_times(conn, alice)
        assert portal and portal[0].endswith("10:30"), \
            f"1: and the portal shows the SAME time, got {portal}"
        assert "08:30" not in portal[0], "1: neither surface may leak the UTC hour"

        # 2. AND IN WINTER, WHERE THE OFFSET IS DIFFERENT. a single fixed shift
        # would be right for one season and an hour out for the other - the
        # defect that is invisible until March.
        appointments.book(conn, bob, "dentist", f"{WINTER}T10:30", 30)
        winter_row = conn.execute(
            "SELECT starts_at FROM appointments WHERE patient_id = ?", (bob,)).fetchone()
        assert winter_row["starts_at"] == f"{WINTER}T09:30:00+00:00", \
            f"2: CET is +1, got {winter_row['starts_at']}"
        assert _agenda_times(conn, WINTER) == ["10:30"], "2: and the clinic still sees 10:30"
        assert _portal_times(conn, bob)[0].endswith("10:30"), "2: on both surfaces"

        # 3. THE LATE SLOT. 23:30 local in summer is stored on the PREVIOUS
        # UTC date. a read path that sliced the stored text would file it under
        # the wrong day and it would vanish from the agenda it belongs to.
        appointments.book(conn, alice, "dr_ferrari", f"{SUMMER}T18:30", 30)
        late = conn.execute(
            "SELECT starts_at FROM appointments WHERE dentist = 'dr_ferrari'").fetchone()
        assert clinic_time.local_date(late["starts_at"]) == SUMMER, "3: it is a summer day booking"
        counts = appointments.month_counts(conn, "2026-09-01", "2026-10-01")
        assert counts.get(SUMMER, {}).get("booked") == 2, \
            f"3: the month grid must count both of that day's bookings, got {counts.get(SUMMER)}"

        # 4. PATIENT ISOLATION SURVIVED THE CONVERSION. the read paths were
        # rewritten; the scoping in them must not have been lost on the way.
        alice_booked, _ = appointments.open_for_patient(conn, alice)
        bob_booked, _ = appointments.open_for_patient(conn, bob)
        assert all(r["patient_id"] == alice for r in alice_booked), "4: alice sees only alice"
        assert all(r["patient_id"] == bob for r in bob_booked), "4: bob sees only bob"
        assert len(bob_booked) == 1 and len(alice_booked) == 2, \
            f"4: {len(alice_booked)} / {len(bob_booked)}"

        # 5. A REQUEST IS STILL A DATE. it went through none of the conversion,
        # and the surfaces must still refuse to print an hour for it.
        soon = "2026-12-02"      # a Wednesday, and not one of the demo closures
        rid = appointments.request(conn, bob, soon, appointments.MORNING)
        req = conn.execute("SELECT starts_at, period FROM appointments WHERE id = ?",
                           (rid,)).fetchone()
        assert req["starts_at"] == f"{soon}T00:00:00", \
            f"5: a request carries a bare date marker, got {req['starts_at']}"
        assert not clinic_time.has_offset(req["starts_at"]), "5: and never an offset"

        # 6. CONFIRMING IT MAKES IT AN INSTANT, and the schedule rule that
        # governs a booking governs the confirm too.
        try:
            appointments.confirm(conn, rid, "dentist", f"{soon}T03:00", 30)
            raise AssertionError("6: confirming outside opening hours must be refused")
        except ValueError as e:
            assert "opens at" in str(e), f"6: for the stated reason, got {e}"
        appointments.confirm(conn, rid, "dentist", f"{soon}T10:00", 30)
        confirmed = conn.execute("SELECT starts_at, status FROM appointments WHERE id = ?",
                                 (rid,)).fetchone()
        assert confirmed["status"] == "booked", "6: a confirmed request is booked"
        assert clinic_time.has_offset(confirmed["starts_at"]), \
            "6: and its start is now a real instant"
        assert clinic_time.local_hhmm(confirmed["starts_at"]) == "10:00", \
            "6: at the time staff chose"

        # 7. THE DST BOUNDARY IS REFUSED, NOT GUESSED. 02:30 on the fall-back
        # Sunday is two real moments an hour apart. the clinic is shut then, so
        # this is a guard on the guard - but it must refuse rather than pick.
        try:
            appointments.book(conn, alice, "dentist", "2026-10-25T02:30", 30)
            raise AssertionError("7: an ambiguous local time must not be booked")
        except ValueError:
            pass
        try:
            appointments.book(conn, alice, "dentist", "2026-03-29T02:30", 30)
            raise AssertionError("7: a nonexistent local time must not be booked")
        except ValueError:
            pass

        # 8. THE CIVIL-TIME FIXTURES NEVER MOVED. the demo schedule is wall
        # time, and a conversion that touched it would shift the clinic's
        # opening hours by an hour every summer.
        opens = {r["weekday"]: r["opens"]
                 for r in conn.execute("SELECT weekday, opens FROM clinic_hours")}
        assert opens[0] == "09:00" and opens[5] == "09:00", f"8: {opens}"
        sat = availability.hours_for(conn, 5)
        assert sat["closes"] == "13:00", "8: saturday is still a morning clinic"

        # 9. AND A HOLIDAY IS STILL A DATE. the august closure and the leave
        # range are dates, and booking into one is refused by the date, not by
        # an instant comparison that would drift at the edges.
        why = availability.refusal(conn, "dentist", "2026-08-15T10:00", 30)
        assert why and "closed" in why, f"9: ferragosto must be closed, got {why}"
        leave = availability.refusal(conn, "dentist", "2026-08-12T10:00", 30)
        assert leave and "chiusura estiva" in leave, f"9: the august leave applies, got {leave}"
        conn.close()

    print("selftest ok")


def main():
    if "--selftest" in sys.argv:
        real = clinic_time.now
        clinic_time.now = lambda env=None: TODAY
        try:
            selftest()
        finally:
            clinic_time.now = real
        return
    print("usage: python p52_acceptance_selftest.py --selftest")


if __name__ == "__main__":
    main()
