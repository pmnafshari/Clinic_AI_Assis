"""the Italian demo-data contract (P22), checked, not assumed.

    .venv/bin/python demo_contract.py [--db db/clinic.sqlite]

for every patient: a checksum-valid codice fiscale (real 16-character shape,
DM 23/12/1976), unique ignoring case; no second identity with the same name
ignoring case; a phone that is None or canonical E.164; a name that is not all
lower / upper case; coherent appointments (a known status, a dentist and a
length when booked, an instant stored with its offset); no visit dated after the
clinic's today. seeded demo identities (demo_identities) must have no violation;
older identities are reported with theirs and classified - this tool changes
nothing.
"""

import json
import sys

import clinic_time
import codice_fiscale
import phones
from shared.names import person

STATUSES = ("booked", "cancelled", "requested", "declined")


def check(conn):
    rows = conn.execute("SELECT patient_id, codice_fiscale, patient_name, phone FROM patients").fetchall()
    demo = {r[0] for r in conn.execute("SELECT patient_id FROM demo_identities")} if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'demo_identities'").fetchone() else set()
    by_cf, by_name = {}, {}
    for r in rows:
        by_cf.setdefault((r["codice_fiscale"] or "").upper(), []).append(r["patient_id"])
        by_name.setdefault(" ".join((r["patient_name"] or "").lower().split()), []).append(r["patient_id"])
    today = clinic_time.now().date().isoformat()
    out = []
    for r in rows:
        v = []
        cf = r["codice_fiscale"] or ""
        if not codice_fiscale.is_real(cf):
            v.append("codice fiscale is not checksum-valid (synthetic shape)")
        if len(by_cf[cf.upper()]) > 1:
            v.append("codice fiscale repeated (ignoring case)")
        if len(by_name[" ".join((r["patient_name"] or "").lower().split())]) > 1:
            v.append("another identity has the same name (ignoring case)")
        if r["phone"] is not None and r["phone"] != phones.canonical(r["phone"]):
            v.append("phone is not a valid Italian number in E.164")
        if r["patient_name"] and person(r["patient_name"]) != r["patient_name"]:
            v.append("name casing (all lower or all upper case)")
        for a in conn.execute("SELECT status, dentist, minutes, starts_at FROM appointments WHERE patient_id = ?",
                              (r["patient_id"],)):
            if a["status"] not in STATUSES:
                v.append(f"appointment with unknown status {a['status']!r}")
            elif a["status"] == "booked" and (not a["dentist"] or not a["minutes"]
                                              or not clinic_time.has_offset(a["starts_at"])):
                v.append("booked appointment without a dentist, a length or an offset")
        future = conn.execute("SELECT COUNT(*) FROM visits WHERE patient_id = ? AND visit_date > ?",
                              (r["patient_id"], today)).fetchone()[0]
        if future:
            v.append(f"{future} visit(s) dated after today")
        out.append({"patient_id": r["patient_id"], "codice_fiscale": cf, "name": r["patient_name"],
                    "demo": r["patient_id"] in demo, "violations": v})
    return out


def main(argv):
    import sqlite3
    db = argv[argv.index("--db") + 1] if "--db" in argv else "db/clinic.sqlite"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        report = check(conn)
    finally:
        conn.close()
    print(json.dumps(report, indent=1))
    return 1 if any(r["violations"] for r in report if r["demo"]) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
