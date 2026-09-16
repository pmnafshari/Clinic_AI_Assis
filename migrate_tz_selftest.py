"""Phase 52 preflight proof, on synthetic fixtures only.

The interesting cases are the ones the preflight must REFUSE to resolve: a
local time that never happened, and one that happened twice. Both are real
rows a clinic can hold, and both are a question for a person.
"""

import sys
import tempfile
from pathlib import Path

import clinic_time
import migrate_tz
import patient_id
import storage

ROME = "Europe/Rome"


def selftest():
    with tempfile.TemporaryDirectory() as tmp:
        conn = storage.init_db(str(Path(tmp) / "tz.sqlite"))
        pid = patient_id.seed_patient(conn, "RSSM800010150100", "Mario Rossi")

        def appt(starts_at, status="booked", dentist="dr rossi"):
            conn.execute(
                "INSERT INTO appointments (patient_id, dentist, starts_at, minutes, status,"
                " created_at, updated_at) VALUES (?, ?, ?, 30, ?, '2026-01-01T08:00:00',"
                " '2026-01-01T08:00:00')", (pid, dentist, starts_at, status))

        appt("2026-01-15T09:00:00")                      # winter, CET
        appt("2026-07-15T09:00:00", dentist="dr b")      # summer, CEST
        appt("2026-03-29T02:30:00", dentist="dr c")      # NEVER HAPPENED
        appt("2026-10-25T02:30:00", dentist="dr d")      # HAPPENED TWICE
        appt("2026-08-01T00:00:00", status="requested", dentist="")   # a DATE
        conn.commit()

        # 1. an invalid source zone is refused before anything is read
        bad = migrate_tz.preflight(conn, "Mars/Olympus")
        assert bad["blockers"] and "IANA" in bad["blockers"][0], f"1: {bad}"

        r = migrate_tz.preflight(conn, ROME)

        # 2. THE PREFLIGHT MUTATES NOTHING. a dry run that changes anything is
        # not a dry run.
        still = conn.execute(
            "SELECT starts_at FROM appointments ORDER BY id").fetchall()
        assert still[0]["starts_at"] == "2026-01-15T09:00:00", "2: nothing may be rewritten"
        assert migrate_tz.preflight(conn, ROME)["fields"] == r["fields"], "2: and it is stable"

        # 3. the two DST rows are reported, named, and NOT resolved
        whys = {u["why"] for u in r["unresolved"]}
        assert "nonexistent" in whys and "ambiguous" in whys, f"3: {r['unresolved']}"
        assert r["blockers"], "3: unresolved rows must block the conversion"
        vals = {u["value"] for u in r["unresolved"]}
        assert "2026-03-29T02:30:00" in vals and "2026-10-25T02:30:00" in vals, f"3: {vals}"

        # 4. the ordinary rows preview their destination, and the offset is
        # seasonal - a single fixed offset would get one of these wrong
        preview = {p["from_local"]: p["to_utc"] for p in r["preview"] if p["to_utc"]}
        assert preview["2026-01-15T09:00:00"].startswith("2026-01-15T08:00"), \
            f"4: winter is +1, got {preview.get('2026-01-15T09:00:00')}"
        assert preview["2026-07-15T09:00:00"].startswith("2026-07-15T07:00"), \
            f"4: summer is +2, got {preview.get('2026-07-15T09:00:00')}"

        # 5. A REQUEST IS A DATE AND STAYS ONE. it is counted, not converted,
        # and it never appears in the booked preview.
        assert r["requested_left_as_dates"] == 1, f"5: {r['requested_left_as_dates']}"
        assert all(p["from_local"] != "2026-08-01T00:00:00" for p in r["preview"]), \
            "5: a requested row must never be previewed as an instant"

        # 6. and the fields deliberately left alone are stated out loud, so a
        # reader does not have to wonder whether they were missed
        left = {row[0] for row in migrate_tz.LEFT_ALONE}
        assert {"clinic_hours", "dentist_schedule", "visits", "clinic_closures"} <= left, \
            f"6: {left}"

        # 7. a row that already carries an offset is recognised, not converted
        # twice - the conversion has to be safe to re-run
        conn.execute("UPDATE appointments SET starts_at = '2026-01-15T08:00:00+00:00'"
                     " WHERE starts_at = '2026-01-15T09:00:00'")
        conn.commit()
        again = migrate_tz.preflight(conn, ROME)
        tally = again["fields"]["appointments.starts_at (booked only)"]
        assert tally.get("already_aware") == 1, f"7: {tally}"

    # ---- the conversion itself ------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        db = str(root / "conv.sqlite")
        conn = storage.init_db(db)
        pid = patient_id.seed_patient(conn, "RSSM800010150100", "Mario Rossi")
        backup = root / "backup.cbk"
        backup.write_text("stand-in for a real encrypted backup")

        def appt(starts_at, status="booked", dentist="dr rossi"):
            cur = conn.execute(
                "INSERT INTO appointments (patient_id, dentist, starts_at, minutes, status,"
                " created_at, updated_at) VALUES (?, ?, ?, 30, ?, '2026-01-01T08:00:00',"
                " '2026-01-01T08:00:00')", (pid, dentist, starts_at, status))
            return cur.lastrowid

        winter = appt("2026-01-15T09:00:00")
        summer = appt("2026-07-15T09:00:00", dentist="dr b")
        req = appt("2026-08-01T00:00:00", status="requested", dentist="")
        conn.execute("INSERT INTO audit_log (ts, username, role, action, target, allowed)"
                     " VALUES ('2026-07-15T09:00:00', 'u', 'dentist', 'x', NULL, 1)")
        conn.execute("INSERT INTO clinic_hours (weekday, opens, closes, closed, capacity)"
                     " VALUES (0, '09:00', '18:00', 0, 0)")
        conn.execute("INSERT INTO visits (patient_id, visit_date, procedures, source_path)"
                     " VALUES (?, '2026-07-15', 'prophy', 'x.txt')", (pid,))
        conn.commit()

        def starts(appt_id):
            return conn.execute("SELECT starts_at FROM appointments WHERE id = ?",
                                (appt_id,)).fetchone()["starts_at"]

        # 8. A BACKUP IS MANDATORY, and a named file that does not exist is not
        # a backup. this rewrites what every timestamp means and has no inverse.
        for bad_backup in (None, "", str(root / "nope.cbk")):
            refused = migrate_tz.convert(conn, ROME, backup_path=bad_backup)
            assert not refused["ok"] and refused["blockers"], f"8: {bad_backup!r} -> {refused}"
        assert starts(winter) == "2026-01-15T09:00:00", "8: and nothing moved"

        # 9. AN UNRESOLVED ROW ABORTS THE WHOLE RUN. converting the rest and
        # leaving that one is how a database ends up half in each contract.
        dst = appt("2026-10-25T02:30:00", dentist="dr d")
        conn.commit()
        blocked = migrate_tz.convert(conn, ROME, backup_path=str(backup))
        assert not blocked["ok"], "9: an ambiguous row must block the run"
        assert starts(winter) == "2026-01-15T09:00:00", "9: and nothing may have moved"
        assert starts(summer) == "2026-07-15T09:00:00", "9: not one row"
        conn.execute("DELETE FROM appointments WHERE id = ?", (dst,))
        conn.commit()

        # 10. the real thing. both seasons get their OWN offset - a single
        # fixed shift would be right for one of them and an hour out for the
        # other, which is the defect this whole phase exists to prevent.
        result = migrate_tz.convert(conn, ROME, backup_path=str(backup))
        assert result["ok"], f"10: {result}"
        assert starts(winter) == "2026-01-15T08:00:00+00:00", f"10: CET is +1, got {starts(winter)}"
        assert starts(summer) == "2026-07-15T07:00:00+00:00", f"10: CEST is +2, got {starts(summer)}"

        # 11. AND THE THREE KINDS THAT MUST NOT MOVE DID NOT. a request is a
        # date and a period, opening hours are civil time, a visit is a date.
        assert starts(req) == "2026-08-01T00:00:00", "11: a request has no time to convert"
        assert conn.execute("SELECT opens FROM clinic_hours WHERE weekday = 0").fetchone()[0] \
            == "09:00", "11: the clinic still opens at 09:00, in every season"
        assert conn.execute("SELECT visit_date FROM visits").fetchone()[0] == "2026-07-15", \
            "11: a visit date is a date"

        # 12. machine instants moved too, on their own offset
        assert conn.execute("SELECT ts FROM audit_log").fetchone()[0] \
            == "2026-07-15T07:00:00+00:00", "12: audit_log.ts is an instant"

        # 13. IDEMPOTENT. a second run is a no-op, not a second shift. this is
        # the one that turns an interrupted migration into "run it again"
        # rather than a restore.
        second = migrate_tz.convert(conn, ROME, backup_path=str(backup))
        assert second["ok"], f"13: {second}"
        assert starts(winter) == "2026-01-15T08:00:00+00:00", "13: a re-run must not shift again"
        assert starts(summer) == "2026-07-15T07:00:00+00:00", "13: nor this one"
        assert conn.execute("SELECT ts FROM audit_log").fetchone()[0] \
            == "2026-07-15T07:00:00+00:00", "13: nor the audit row"

        # 14. the converted database now satisfies the startup contract, which
        # is the real proof that the guard and the migration agree
        assert clinic_time.contract_refusal(conn) is None, \
            f"14: a converted database must start, got {clinic_time.contract_refusal(conn)}"

        # 15. the run is recorded in the durable ledger, so an interrupted
        # migration can be resumed and an operator can see what happened
        ops = conn.execute(
            "SELECT subject, state FROM migration_ops WHERE migration = ?",
            (migrate_tz.MIGRATION,)).fetchall()
        assert ops, "15: the conversion must leave a ledger"
        assert all(r["state"] == "done" for r in ops), f"15: {[dict(r) for r in ops]}"
        assert any(r["subject"] == "appointments.starts_at(booked)" for r in ops), \
            "15: including the appointment step"
        conn.close()

    # 16. FAILURE LEAVES NOTHING HALF-CONVERTED. the write is one transaction,
    # so an error partway through rolls the whole thing back rather than
    # leaving some columns UTC and some local.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        conn = storage.init_db(str(root / "fail.sqlite"))
        pid = patient_id.seed_patient(conn, "RSSM800010150100", "Mario Rossi")
        backup = root / "backup.cbk"
        backup.write_text("stand-in")
        conn.execute(
            "INSERT INTO appointments (patient_id, dentist, starts_at, minutes, status,"
            " created_at, updated_at) VALUES (?, 'dr rossi', '2026-01-15T09:00:00', 30,"
            " 'booked', '2026-01-01T08:00:00', '2026-01-01T08:00:00')", (pid,))
        conn.execute("INSERT INTO audit_log (ts, username, role, action, target, allowed)"
                     " VALUES ('2026-07-15T09:00:00', 'u', 'dentist', 'x', NULL, 1)")
        conn.commit()

        real_to_storage = clinic_time.to_storage
        calls = {"n": 0}

        def explode(instant):
            # fail partway: the first field converts, a later one blows up
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("disk gave out mid-migration")
            return real_to_storage(instant)

        clinic_time.to_storage = explode
        try:
            broken = migrate_tz.convert(conn, ROME, backup_path=str(backup))
        finally:
            clinic_time.to_storage = real_to_storage
        assert not broken["ok"], "16: a failed run must report failure"
        assert conn.execute("SELECT ts FROM audit_log").fetchone()[0] \
            == "2026-07-15T09:00:00", "16: and roll back every column it had touched"
        assert conn.execute("SELECT starts_at FROM appointments").fetchone()[0] \
            == "2026-01-15T09:00:00", "16: including the appointment"

        # and the retry after the fault succeeds completely
        retry = migrate_tz.convert(conn, ROME, backup_path=str(backup))
        assert retry["ok"], f"16: the retry must succeed, got {retry}"
        assert conn.execute("SELECT ts FROM audit_log").fetchone()[0] \
            == "2026-07-15T07:00:00+00:00", "16: and finish the job"
        conn.close()

    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
