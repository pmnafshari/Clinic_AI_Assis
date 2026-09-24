"""migration M22 (P22): proven demo-data defects only, with before/after counts.

    .venv/bin/python migrate_p22.py            # dry run: what would change
    .venv/bin/python migrate_p22.py --apply    # after a verified backup

1. phones to E.164; a stored value that is not a valid Italian number becomes
   NULL (it was never dialable) and is listed by patient id.
2. the one proven seed fixture with an all-lower-case name (seed2.json,
   `paola rossi`) is title-cased. other names are left as staff curated them.
3. the deterministic demo cohort is seeded (demo_seed, seed 22).
nothing is deleted. the probable duplicate Paola Rossi / paola rossi (codici
fiscali one transposition apart) is left for the human duplicate review; legacy
codici fiscali are not rewritten (the review package and hand-typed notes refer
to them) - demo_contract.py reports them.
"""

import json
import sys

import phones
from shared.names import person

PROVEN_FIXTURE_NAMES = {"seed2.json"}      # source files of rows a seed script created


def plan(conn):
    changes = {"phones_canonicalised": [], "phones_cleared": [], "names": []}
    for r in conn.execute("SELECT patient_id, phone FROM patients WHERE phone IS NOT NULL"):
        good = phones.canonical(r["phone"])
        if good is None:
            changes["phones_cleared"].append(r["patient_id"])
        elif good != r["phone"]:
            changes["phones_canonicalised"].append({"patient_id": r["patient_id"], "to": good})
    for r in conn.execute("SELECT p.patient_id, p.patient_name, v.source_path FROM patients p"
                          " JOIN visits v ON v.patient_id = p.patient_id"):
        if r["source_path"].rsplit("/", 1)[-1] in PROVEN_FIXTURE_NAMES and person(r["patient_name"]) != r["patient_name"]:
            changes["names"].append({"patient_id": r["patient_id"], "to": person(r["patient_name"])})
    return changes


def apply(conn, changes, seed=22, anchor="2026-10-05"):
    import demo_seed
    for c in changes["phones_canonicalised"]:
        conn.execute("UPDATE patients SET phone = ? WHERE patient_id = ?", (c["to"], c["patient_id"]))
    for pid in changes["phones_cleared"]:
        conn.execute("UPDATE patients SET phone = NULL WHERE patient_id = ?", (pid,))
    for c in changes["names"]:
        conn.execute("UPDATE patients SET patient_name = ? WHERE patient_id = ?", (c["to"], c["patient_id"]))
    conn.commit()
    return demo_seed.seed(conn, seed=seed, anchor=anchor)


def main(argv):
    import demo_contract
    import demo_seed
    import storage
    conn = storage.init_db("db/clinic.sqlite")
    try:
        before = demo_seed.counts(conn)
        changes = plan(conn)
        out = {"before": before, "changes": changes}
        if "--apply" in argv:
            out["seeded"] = apply(conn, changes)
            out["after"] = demo_seed.counts(conn)
            out["contract"] = demo_contract.check(conn)
        print(json.dumps(out, indent=1))
    finally:
        conn.close()
    return 0


def selftest():
    import tempfile
    from pathlib import Path

    import demo_fixtures
    import demo_seed
    import patient_id
    import storage
    with tempfile.TemporaryDirectory() as tmp:
        conn = storage.init_db(str(Path(tmp) / "m.sqlite"))
        demo_fixtures.seed(conn)
        giulia = patient_id.seed_patient(conn, "ZZDM850010150601", "Giulia Esposito", "3478801234")
        paola = patient_id.seed_patient(conn, "RSSP850010150900", "paola rossi", "555 0000")
        other = patient_id.seed_patient(conn, "RSPS850010150900", "Paola Rossi")
        mario = patient_id.seed_patient(conn, "RSSI800010150100", "mario rossi")      # lower case, NOT a fixture
        for pid, src in ((paola, "RSSP850010150900/notes/seed2.json"), (mario, "x/notes/web.json")):
            conn.execute("INSERT INTO visits (patient_id, source_path, procedures) VALUES (?, ?, '[]')", (pid, src))
        conn.commit()
        c = plan(conn)
        assert c["phones_canonicalised"] == [{"patient_id": giulia, "to": "+393478801234"}], c
        assert c["phones_cleared"] == [paola], c
        assert c["names"] == [{"patient_id": paola, "to": "Paola Rossi"}], "only the proven fixture is renamed"
        before = demo_seed.counts(conn)
        apply(conn, c, anchor="2030-10-07")
        row = lambda pid: conn.execute("SELECT patient_name, phone FROM patients WHERE patient_id = ?", (pid,)).fetchone()  # noqa: E731
        assert tuple(row(paola)) == ("Paola Rossi", None) and row(giulia)["phone"] == "+393478801234"
        assert row(mario)["patient_name"] == "mario rossi", "a non-fixture name must not be touched"
        assert row(other)["patient_name"] == "Paola Rossi", "the duplicate is left for the human review"
        after = demo_seed.counts(conn)
        assert after["patients"] == before["patients"] + 8 and after["demo_identities"] == 8, (before, after)
        assert plan(conn) == {"phones_canonicalised": [], "phones_cleared": [], "names": []}, "not idempotent"
        apply(conn, plan(conn), anchor="2030-10-07")
        assert demo_seed.counts(conn) == after, "a second run changed counts"
        conn.close()
    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        sys.exit(main(sys.argv[1:]))
