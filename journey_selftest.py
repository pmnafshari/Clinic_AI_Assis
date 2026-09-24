"""P17.T1: one patient, the whole way through, on the real functions.

public request -> staff confirm -> consent -> the dentist's typed note with its
invoice lines -> invoice issued -> two installments -> a desk payment -> the
follow-up booked -> reminders planned -> the portal shows her, and only her.

the blocked parts stay blocked and are asserted as blocked, never skipped: no
messaging provider (a due reminder is held, nothing sent) and no payment
provider (a pay link is refused). synthetic patients, a temporary database and
index, and a pinned clinic clock (the p52 lesson: "in the future" needs a clock).
"""

import sys
import tempfile
from datetime import datetime
from pathlib import Path

import appointments
import clinic_time
import consent
import demo_fixtures
import ledger
import patient_accessor
import patient_id
import payments
import providers
import reminders
import storage
from dental_notes_schema import DentalNote, Invoice

TODAY = datetime(2026, 9, 17, 9, 0)         # a Thursday, clinic time
VISIT_DAY = "2026-09-23"                    # a Wednesday, inside the demo hours
FOLLOW_UP = "2026-10-21"                    # four weeks later, still CEST
CF, OTHER_CF = "ZZJN800101010101", "ZZJN800101010102"


def selftest():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        conn = storage.init_db(str(tmp / "journey.sqlite"))
        demo_fixtures.seed(conn)
        pid = patient_id.seed_patient(conn, CF, "Jana Journey")
        other = patient_id.seed_patient(conn, OTHER_CF, "Otto Other")
        conn.execute("UPDATE patients SET phone = '3330000001' WHERE patient_id = ?", (pid,))
        conn.commit()

        # 1. the patient asks from the portal; staff give it a dentist and a time
        req = appointments.request(conn, pid, VISIT_DAY, "morning", "controllo")
        assert [r["id"] for r in appointments.pending_requests(conn)] == [req], "1: the request is queued"
        appointments.confirm(conn, req, "dentist", f"{VISIT_DAY}T10:30", 30)
        booked, asked = appointments.open_for_patient(conn, pid)
        assert [b["id"] for b in booked] == [req] and asked == [], "1: confirmed, and no longer a request"
        assert appointments.open_for_patient(conn, other) == ([], []), "1: nobody else sees it"

        # 2. at the desk: consent to messages and to the assistant, recorded, not assumed
        assert not consent.allows(conn, pid, "messaging"), "2: no consent until it is given"
        consent.record(conn, pid, "messaging", True, "assistant", "assistant")
        consent.record(conn, pid, "ai_assistant", True, "assistant", "assistant")
        assert consent.allows(conn, pid, "messaging")

        # 3. the dentist types the visit; its invoice lines come with it
        note = DentalNote(patient_name="Jana Journey", codice_fiscale=CF, phone="3330000001",
                          visit_date=VISIT_DAY, procedures=["filling 36"],
                          invoices=[Invoice(amount=120.0, description="otturazione 36"),
                                    Invoice(amount=60.0, description="igiene")],
                          clinical_notes="otturazione composito dente 36, controllo tra un mese",
                          next_appointment=FOLLOW_UP)
        collection = storage.get_collection(str(tmp / "chroma"))
        assert storage.save_new_note(note, conn, collection, "dentist", "dentist",
                                     sorted_root=tmp / "sorted") != "failed", "3: the note is filed"
        visit = conn.execute("SELECT id FROM visits WHERE patient_id = ?", (pid,)).fetchone()["id"]
        assert collection.get(where={"codice_fiscale": CF})["ids"], "3: and searchable for staff"
        invoice = conn.execute("SELECT id FROM billing_invoices WHERE visit_id = ?", (visit,)).fetchone()["id"]
        assert ledger.summary(conn, invoice)["state"] == "draft", "3: a draft is not owed yet"

        # 4. invoice issued, split in two, the first half paid at the desk
        ledger.issue(conn, invoice, "dentist", "dentist")
        s = ledger.summary(conn, invoice)
        assert (s["state"], s["total_cents"], s["outstanding_cents"]) == ("issued", 18000, 18000), s
        ledger.plan_installments(conn, invoice, 2, VISIT_DAY, "dentist", "dentist")
        ledger.record_payment(conn, invoice, "90,00", "card", "journey-1", "assistant", "assistant")
        s = ledger.summary(conn, invoice)
        assert (s["state"], s["paid_cents"], s["outstanding_cents"]) == ("partially_paid", 9000, 9000), s

        # 5. no payment provider: a pay link is refused, not faked
        refused = None
        try:
            payments.create_link(conn, invoice, "assistant", "assistant", env={})
        except Exception as e:
            refused = e
        # refused BY THE GATE: any other error means the gate let it through and something later broke
        assert isinstance(refused, providers.ProviderError), f"5: not refused by the provider gate: {refused!r}"
        assert conn.execute("SELECT COUNT(*) FROM payment_links").fetchone()[0] == 0, "5: a link row exists"
        assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'payment_link' AND allowed = 0"
                            ).fetchone()[0] == 1, "5: the refusal was not audited"

        # 6. follow-up booked; reminders planned for it and for the money
        follow = appointments.book(conn, pid, "dentist", f"{FOLLOW_UP}T10:30", 30)
        now = clinic_time.read_instant("2026-09-24T08:00:00+00:00")
        reminders.plan(conn, now=now)
        kinds = sorted(r["kind"] for r in conn.execute(
            "SELECT kind FROM reminder_jobs WHERE patient_id = ? AND status = 'scheduled'", (pid,)))
        assert "appointment" in kinds and "invoice" in kinds, f"6: planned {kinds}"

        # 7. no messaging provider: due reminders are held, nothing sent, nothing claimed
        later = clinic_time.read_instant(f"{FOLLOW_UP}T06:00:00+00:00")
        class WatchedDisabled(reminders.DisabledTransport):
            # still the disabled transport; it only notes whether anyone tried to send through it
            def __init__(self):
                self.asked = []

            def send(self, phone, body, key):
                self.asked.append(key)

        transport = WatchedDisabled()
        report = reminders.run_once(conn, transport, now=later)
        assert transport.asked == [], f"7: the disabled transport was asked to send {len(transport.asked)} time(s)"
        assert report["transport"] == "disabled" and "provider" in report["reason"], report
        assert conn.execute("SELECT COUNT(*) FROM reminder_jobs WHERE status != 'scheduled'"
                            " AND patient_id = ?", (pid,)).fetchone()[0] == 0, "7: a reminder moved"
        assert conn.execute("SELECT COUNT(*) FROM delivery_receipts").fetchone()[0] == 0, "7: nothing went out"

        # 8. her portal: the follow-up and what she owes; the other patient sees none of it
        # P22: the next appointment is the first booking, not the note's follow-up text
        assert patient_accessor.get_next_appointment(pid, conn) == f"{VISIT_DAY} 10:30", "8: not the first booking"
        booked, _ = appointments.open_for_patient(conn, pid)
        assert [b["id"] for b in booked] == [req, follow], f"8: her appointments {booked}"
        billing = patient_accessor.get_billing(pid, conn)
        assert billing["outstanding_cents"] == 9000, billing
        assert patient_accessor.get_next_appointment(other, conn) is None, "8: another patient's appointment"
        assert appointments.open_for_patient(conn, other) == ([], []), "8: another patient's bookings"
        assert patient_accessor.get_billing(other, conn)["outstanding_cents"] == 0, "8: another patient's debt"
        conn.close()
    print("selftest ok")


def main():
    if "--selftest" not in sys.argv:
        print("usage: python journey_selftest.py --selftest")
        return
    real = clinic_time.now
    clinic_time.now = lambda env=None: TODAY
    try:
        selftest()
    finally:
        clinic_time.now = real


if __name__ == "__main__":
    main()
