"""DEMO billing fixtures - synthetic, labelled, made through the ledger's own API.

NOT REAL MONEY. Every payment is marked source=demo_fixture (or reconciliation)
with a note saying no money moved. They exist so the billing screens, the
portal and the chat have realistic figures to show: one legacy invoice
reconciled as paid, two left unknown for a person to reconcile, and a new
treatment issued, split into installments, with the first one paid.

Idempotent: a second run finds its own keys and changes nothing.

    python demo_billing.py            what it would do
    python demo_billing.py --apply
"""
import json
import sys

import ledger
import patient_id
import storage

DB_PATH = "db/clinic.sqlite"
ACTOR = ("demo_fixture", "dentist")
NOTE = "DEMO fixture - synthetic, no money moved"
DEMO_CF = "ZZDM850010150601"


def _key(conn, key):
    return conn.execute("SELECT 1 FROM payments WHERE idempotency_key = ?", (key,)).fetchone()


def apply(conn):
    done = []
    # a legacy invoice a person checked and found paid
    row = conn.execute(
        "SELECT b.id FROM billing_invoices b JOIN patients p USING (patient_id)"
        " WHERE p.codice_fiscale = 'FRRR850010150200' AND b.state = 'unknown'").fetchone()
    if row:
        ledger.reconcile(conn, row[0], "paid", *ACTOR, "demo-billing-ferrari", method="cash",
                         received_on="2026-06-03", note=NOTE)
        done.append("reconciled Rossana Ferrari's June invoice as paid")

    # a new treatment: implant and crown, issued and split into four
    pid = patient_id.resolve(conn, DEMO_CF)
    if pid is None:
        pid = patient_id.seed_patient(conn, DEMO_CF, "Giulia Esposito", "3478801234")
        vid = conn.execute(
            "INSERT INTO visits (patient_id, visit_date, procedures, clinical_notes,"
            " next_appointment, source_path) VALUES (?, '2026-09-08', ?, ?, '2026-10-08', ?)",
            (pid, '["implant 36", "crown 36"]', "DEMO: implant placed 36, crown to follow",
             "demo/esposito-2026-09-08.json")).lastrowid
        for i, (desc, amount) in enumerate((("impianto 36", 120000), ("corona 36", 65000))):
            conn.execute("INSERT INTO invoices (patient_id, visit_id, line_index, amount,"
                         " amount_cents, description) VALUES (?, ?, ?, ?, ?, ?)",
                         (pid, vid, i, amount / 100, amount, desc))
        inv = ledger.ensure_invoice(conn, pid, vid)
        conn.commit()
        ledger.issue(conn, inv, *ACTOR, due_date="2026-10-08")
        ledger.plan_installments(conn, inv, 4, "2026-10-08", *ACTOR)
        done.append("Giulia Esposito: implant and crown, 1.850,00 in 4 installments")
    inv = conn.execute("SELECT id FROM billing_invoices WHERE patient_id = ?", (pid,)).fetchone()[0]
    if not _key(conn, "demo-billing-esposito-1"):
        ledger.record_payment(conn, inv, "462,50", "card", "demo-billing-esposito-1", *ACTOR,
                              received_on="2026-09-08", reference="DEMO-0001", note=NOTE,
                              source="demo_fixture")
        done.append("Giulia Esposito: first installment paid by card")
    return done


def selftest():
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        conn = storage.init_db(str(Path(tmp) / "clinic.sqlite"))
        pid = patient_id.seed_patient(conn, "FRRR850010150200", "Rossana Ferrari")
        vid = conn.execute("INSERT INTO visits (patient_id, visit_date, procedures,"
                           " clinical_notes, source_path) VALUES (?, '2026-06-03', '[]', '',"
                           " 'f.json')", (pid,)).lastrowid
        conn.execute("INSERT INTO invoices (patient_id, visit_id, line_index, amount)"
                     " VALUES (?, ?, 0, 80.0)", (pid, vid))
        ledger.ensure_invoice(conn, pid, vid, legacy=True)
        conn.commit()

        assert len(apply(conn)) == 3, "1: three fixtures on a fresh database"
        assert apply(conn) == [], "1: a second run changes nothing"
        ferrari = ledger.patient_summary(conn, pid)
        assert ferrari["invoices"][0]["state"] == "paid" and ferrari["outstanding_cents"] == 0
        giulia = ledger.patient_summary(conn, patient_id.resolve(conn, DEMO_CF))
        inv = giulia["invoices"][0]
        assert (inv["total_cents"], inv["paid_cents"], giulia["outstanding_cents"]) == \
            (185000, 46250, 138750), giulia
        assert [i["open_cents"] for i in inv["installments"]] == [0, 46250, 46250, 46250]
        notes = {r[0] for r in conn.execute("SELECT note FROM payments")}
        assert notes == {NOTE}, "2: every fixture payment says it is a demo"

    print("selftest ok")


def main(argv):
    if "--selftest" in argv:
        selftest()
        return
    conn = storage.init_db(DB_PATH)
    try:
        if "--apply" not in argv:
            print("dry run - pass --apply to create the demo billing fixtures")
            return
        print(json.dumps(apply(conn), indent=2))
    finally:
        conn.close()


if __name__ == "__main__":
    main(sys.argv[1:])
