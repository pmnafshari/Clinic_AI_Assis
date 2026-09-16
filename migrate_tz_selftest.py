"""Phase 52 preflight proof, on synthetic fixtures only.

The interesting cases are the ones the preflight must REFUSE to resolve: a
local time that never happened, and one that happened twice. Both are real
rows a clinic can hold, and both are a question for a person.
"""

import sys
import tempfile
from pathlib import Path

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

    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
