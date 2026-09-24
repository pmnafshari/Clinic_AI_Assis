"""P22: the Italian demo-data contract, the deterministic seed, and fail-closed sending.
written before demo_contract.py and demo_seed.py. temporary database, pinned clinic clock.
"""

import sys
import tempfile
from datetime import datetime
from pathlib import Path

import clinic_time
import codice_fiscale
import demo_contract
import demo_fixtures
import demo_seed
import patient_id
import phones
import reminders
import storage

TODAY = datetime(2026, 9, 17, 9, 0)


def selftest():
    # 1. a codice fiscale computed from a fictional identity is structurally and checksum valid,
    #    and encodes that identity (surname, name, birth date, sex, place)
    cf = demo_seed.cf_for("Rossi", "Mario", "1980-01-01", "M", "H501")
    assert cf == "RSSMRA80A01H501U", cf                       # the textbook example
    assert codice_fiscale.is_valid(cf) and codice_fiscale.is_real(cf)
    f = demo_seed.cf_for("Esposito", "Giulia", "1985-10-15", "F", "F839")
    assert f[:6] == "SPSGLI" and f[6:8] == "85" and f[8] == "R" and f[9:11] == "55" and f[11:15] == "F839", f
    assert codice_fiscale.is_valid(f)

    with tempfile.TemporaryDirectory() as tmp:
        conn = storage.init_db(str(Path(tmp) / "demo.sqlite"))
        demo_fixtures.seed(conn)

        # 2. the seed: deterministic, idempotent, contract-clean
        first = demo_seed.seed(conn, seed=22, anchor="2026-10-05")
        counts = demo_seed.counts(conn)
        again = demo_seed.seed(conn, seed=22, anchor="2026-10-05")
        assert demo_seed.counts(conn) == counts, f"2: a rerun added rows: {counts} -> {demo_seed.counts(conn)}"
        assert again["created"] == {"patients": 0, "visits": 0, "appointments": 0, "consents": 0}, again
        assert first["created"]["patients"] >= 6, first
        conn2 = storage.init_db(str(Path(tmp) / "demo2.sqlite"))
        demo_fixtures.seed(conn2)
        demo_seed.seed(conn2, seed=22, anchor="2026-10-05")
        same = lambda c: sorted(tuple(r) for r in c.execute("SELECT codice_fiscale, patient_name, phone FROM patients"))
        assert same(conn) == same(conn2), "2: the same seed gave different identities"
        conn2.close()

        report = demo_contract.check(conn)
        seeded = [r for r in report if r["demo"]]
        assert seeded and all(r["violations"] == [] for r in seeded), [r for r in seeded if r["violations"]]
        cfs = [r["codice_fiscale"] for r in seeded]
        assert len(set(c.upper() for c in cfs)) == len(cfs), "2: duplicate CF in the cohort"
        for row in conn.execute("SELECT p.patient_id, p.patient_name, p.phone FROM patients p"
                                " JOIN demo_identities d ON d.patient_id = p.patient_id"):
            assert row["phone"] is None or row["phone"] == phones.canonical(row["phone"]), row["phone"]
        assert any(r["phone"] is None for r in conn.execute(
            "SELECT phone FROM patients p JOIN demo_identities d ON d.patient_id = p.patient_id")), \
            "2: the contract wants intentional missing values"
        statuses = {r[0] for r in conn.execute("SELECT status FROM appointments")}
        assert {"booked", "cancelled", "requested"} <= statuses, statuses

        # 3. the contract catches what the legacy data had
        legacy = patient_id.seed_patient(conn, "RSSP850010150900", "paola rossi", "555 0000")
        conn.commit()
        bad = {r["patient_id"]: r["violations"] for r in demo_contract.check(conn)}
        v = " ".join(bad[legacy])
        assert "codice fiscale is not checksum-valid" in v and "phone is not a valid" in v and "name casing" in v, v

        # 4. sending fails closed for demo identities, even with a working transport
        demo_pid = conn.execute("SELECT patient_id FROM demo_identities ORDER BY patient_id LIMIT 1").fetchone()[0]
        conn.execute("UPDATE patients SET phone = '+393471234567' WHERE patient_id = ?", (demo_pid,))
        import consent
        consent.record(conn, demo_pid, "messaging", True, "dentist", "dentist")
        conn.commit()
        now = clinic_time.read_instant("2026-09-17T07:00:00+00:00")
        reminders.plan(conn, now=now)
        transport = reminders.TestTransport()
        later = clinic_time.read_instant("2026-10-20T06:00:00+00:00")
        reminders.run_once(conn, transport, now=later)
        sent_to = [s[0] for s in transport.sent] if transport.sent and isinstance(transport.sent[0], tuple) else \
                  [s.get("to") if isinstance(s, dict) else s for s in transport.sent]
        assert "+393471234567" not in str(sent_to), "4: a demo identity was sent a message"
        demo_jobs = conn.execute("SELECT status, cancel_reason FROM reminder_jobs WHERE patient_id = ?",
                                 (demo_pid,)).fetchall()
        assert demo_jobs and all(j["status"] == "cancelled" and j["cancel_reason"] == "demo_identity"
                                 for j in demo_jobs), [tuple(j) for j in demo_jobs]

        # 5. a note with a wrong number never overwrites a good stored one; a stored non-number is never dialled
        from dental_notes_schema import DentalNote
        good = patient_id.seed_patient(conn, "BNCLCU80A01H501Q", "Luca Bianchi", "+393401112233")
        conn.commit()
        storage.upsert_note_sql(DentalNote(patient_name="Luca Bianchi", codice_fiscale="BNCLCU80A01H501Q",
                                           phone="555 0000", procedures=[]), "zz/luca.json", conn)
        assert conn.execute("SELECT phone FROM patients WHERE patient_id = ?", (good,)).fetchone()[0] == "+393401112233", \
            "5: a wrong number in a note overwrote a good one"
        stale = patient_id.seed_patient(conn, "ZZST800101010101", "Stale Phone", "555 0000")
        consent.record(conn, stale, "messaging", True, "dentist", "dentist")
        import appointments
        appointments.book(conn, stale, "dentist", "2026-10-06T10:30", 30)
        conn.commit()
        reminders.plan(conn, now=now)
        t2 = reminders.TestTransport()
        due = conn.execute("SELECT send_at FROM reminder_jobs WHERE patient_id = ?", (stale,)).fetchone()[0]
        reminders.run_once(conn, t2, now=clinic_time.read_instant(due))
        assert "555 0000" not in str(t2.sent), "5: a stored non-number was dialled"
        why = [j["cancel_reason"] for j in conn.execute("SELECT cancel_reason FROM reminder_jobs WHERE patient_id = ?", (stale,))]
        assert why and set(why) == {"no_phone"}, why
        conn.close()
    print("selftest ok")


def main():
    if "--selftest" not in sys.argv:
        print("usage: python demo_data_selftest.py --selftest")
        return
    real = clinic_time.now
    clinic_time.now = lambda env=None: TODAY
    try:
        selftest()
    finally:
        clinic_time.now = real


if __name__ == "__main__":
    main()
