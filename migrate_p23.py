"""migration M23 (P23): the seeded demo appointments' notes carried a codice fiscale
("demo-seed:<CF>:<slot>", written by P22's seed) and every staff screen showed it.
they become the readable, identifier-free tag demo_seed now writes.

    .venv/bin/python migrate_p23.py            # dry run
    .venv/bin/python migrate_p23.py --apply    # after a verified backup

only rows whose patient is listed in demo_identities are touched; nothing else is.
"""

import json
import re
import sys

OLD_BOOKING = re.compile(r"^demo-seed:[A-Z0-9]{16}:(\d+)$")


def plan(conn):
    demo = {r[0]: r[1] for r in conn.execute("SELECT patient_id, seed FROM demo_identities")}
    changes = []
    for r in conn.execute("SELECT id, patient_id, note FROM appointments WHERE note LIKE 'demo-seed%'"):
        if r["patient_id"] not in demo:
            continue
        m = OLD_BOOKING.match(r["note"] or "")
        if m:
            changes.append({"id": r["id"], "to": f"Demo booking (seed {demo[r['patient_id']]}, slot {m.group(1)})"})
        elif r["note"] == "demo-seed request":
            changes.append({"id": r["id"], "to": f"Demo request (seed {demo[r['patient_id']]})"})
    return changes


def apply(conn, changes):
    for c in changes:
        conn.execute("UPDATE appointments SET note = ? WHERE id = ?", (c["to"], c["id"]))
    conn.commit()
    return len(changes)


def main(argv):
    import storage
    conn = storage.init_db("db/clinic.sqlite")
    try:
        before = conn.execute("SELECT COUNT(*) FROM appointments WHERE note LIKE 'demo-seed:%'"
                              " OR note = 'demo-seed request'").fetchone()[0]
        changes = plan(conn)
        out = {"old_tags_before": before, "changes": len(changes)}
        if "--apply" in argv:
            out["applied"] = apply(conn, changes)
            out["old_tags_after"] = conn.execute("SELECT COUNT(*) FROM appointments WHERE note LIKE 'demo-seed:%'"
                                                 " OR note = 'demo-seed request'").fetchone()[0]
            out["appointments_total"] = conn.execute("SELECT COUNT(*) FROM appointments").fetchone()[0]
        print(json.dumps(out, indent=1))
    finally:
        conn.close()
    return 0


def selftest():
    import tempfile
    from pathlib import Path

    import demo_fixtures
    import patient_id
    import storage
    with tempfile.TemporaryDirectory() as tmp:
        conn = storage.init_db(str(Path(tmp) / "m.sqlite"))
        demo_fixtures.seed(conn)
        demo = patient_id.seed_patient(conn, "GLLFNC51R55F839P", "Francesca Gallo")
        other = patient_id.seed_patient(conn, "ZZMO800101010101", "Other Person")
        conn.execute("INSERT INTO demo_identities (patient_id, seed, created_at) VALUES (?, 22, 'x')", (demo,))
        ins = ("INSERT INTO appointments (patient_id, dentist, starts_at, minutes, status, note, created_at, updated_at)"
               " VALUES (?, 'dentist', ?, 30, ?, ?, 'x', 'x')")
        conn.execute(ins, (demo, "2031-01-01T09:00:00+00:00", "booked", "demo-seed:GLLFNC51R55F839P:0"))
        conn.execute(ins, (demo, "2031-01-08T09:00:00+00:00", "cancelled", "demo-seed:GLLFNC51R55F839P:1"))
        conn.execute(ins, (demo, "2031-01-15", "requested", "demo-seed request"))
        conn.execute(ins, (other, "2031-01-02T09:00:00+00:00", "booked", "demo-seed:ZZMO800101010101:0"))
        conn.execute(ins, (demo, "2031-01-03T09:00:00+00:00", "booked", "controllo"))
        conn.commit()
        c = plan(conn)
        assert sorted(x["to"] for x in c) == ["Demo booking (seed 22, slot 0)", "Demo booking (seed 22, slot 1)",
                                              "Demo request (seed 22)"], c
        apply(conn, c)
        notes = {r[0] for r in conn.execute("SELECT note FROM appointments")}
        assert "demo-seed:ZZMO800101010101:0" in notes, "a patient not in demo_identities must not be touched"
        assert "controllo" in notes, "a staff note must not be touched"
        assert not any(n.startswith("demo-seed:GLLF") for n in notes), notes
        assert plan(conn) == [], "not idempotent"
        conn.close()
    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        sys.exit(main(sys.argv[1:]))
