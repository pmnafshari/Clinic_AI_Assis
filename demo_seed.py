"""the deterministic Italian demo cohort (P22). fictional people, never real ones.

    .venv/bin/python demo_seed.py [--seed 22] [--anchor 2026-10-05] [--db db/clinic.sqlite]

- names and surnames are common Italian ones combined at random from a fixed
  seed; birth places are public municipal (Belfiore) codes. the codice fiscale is
  COMPUTED from that fictional identity (DM 23/12/1976) - checksum-valid and
  consistent with the name, birth date, sex and place, but issued by nobody and
  never presented as a real person's code.
- phones are Italian mobiles in E.164; two identities have none on purpose.
  there is no reserved "fictional" Italian mobile range, so a number here could
  exist: every seeded identity is listed in demo_identities, and reminders refuse
  to send to anyone listed there (fail closed), provider or no provider.
- patients have no address or e-mail field in this schema; nothing is invented
  for them. if such fields are added, e-mails use the reserved example.test.
- appointments go through appointments.book / request / cancel, so every one is
  on a dentist's roster, inside the clinic's hours and never overlapping; each
  seeded row carries a "demo-seed:" tag, which is what makes a rerun add nothing.
- the anchor is the first working day the bookings start from; the default is
  fixed so the cohort is the same on every machine. rerun with a later anchor to
  refresh it (old demo bookings stay as they were).
"""

import json
import random
import sys
import unicodedata
from datetime import date, timedelta

import clinic_time
import codice_fiscale

SCHEMA = """
CREATE TABLE IF NOT EXISTS demo_identities (
    patient_id TEXT PRIMARY KEY,
    seed INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
"""

SURNAMES = ["Bianchi", "Romano", "Colombo", "Ricci", "Marino", "Greco", "Bruno", "Gallo", "Conti", "De Luca",
            "Mancini", "Costa", "Giordano", "Rizzo", "Lombardi", "Moretti", "Barbieri", "Fontana", "Santoro",
            "Mariani", "Rinaldi", "Caruso", "Ferrara", "Galli"]
MALE = ["Luca", "Marco", "Andrea", "Francesco", "Alessandro", "Matteo", "Lorenzo", "Davide", "Simone", "Riccardo"]
FEMALE = ["Giulia", "Chiara", "Francesca", "Sara", "Martina", "Elena", "Valentina", "Alessia", "Federica", "Silvia"]
PLACES = [("H501", "Roma"), ("F205", "Milano"), ("F839", "Napoli"), ("L219", "Torino"), ("D612", "Firenze"),
          ("A944", "Bologna"), ("G273", "Palermo"), ("D969", "Genova")]
MONTHS = "ABCDEHLMPRST"
PROCEDURES = [(["prophy"], "igiene professionale, gengive in ordine", 60.0),
              (["filling 36"], "otturazione composito dente 36", 120.0),
              (["rct 26"], "devitalizzazione 26, prima seduta", 250.0),
              (["crown 21"], "impronte per corona 21", 180.0)]
COHORT_SIZE = 8
NO_PHONE = {2, 5}                     # intentional missing values


def _letters(s):
    s = unicodedata.normalize("NFD", s.upper())
    return "".join(c for c in s if "A" <= c <= "Z")


def _split(s):
    s = _letters(s)
    return [c for c in s if c not in "AEIOU"], [c for c in s if c in "AEIOU"]


def _surname_code(surname):
    cons, vow = _split(surname)
    return ("".join(cons + vow) + "XXX")[:3]


def _name_code(name):
    cons, vow = _split(name)
    if len(cons) >= 4:
        return cons[0] + cons[2] + cons[3]
    return ("".join(cons + vow) + "XXX")[:3]


def cf_for(surname, name, birth, sex, place):
    b = date.fromisoformat(birth)
    day = b.day + (40 if sex == "F" else 0)
    first15 = f"{_surname_code(surname)}{_name_code(name)}{b.year % 100:02d}{MONTHS[b.month - 1]}{day:02d}{place}"
    return first15 + codice_fiscale.check_char(first15)


def cohort(seed):
    rng = random.Random(seed)
    people, seen = [], set()
    while len(people) < COHORT_SIZE:
        i = len(people)
        sex = "F" if i % 2 == 0 else "M"
        name = rng.choice(FEMALE if sex == "F" else MALE)
        surname = rng.choice(SURNAMES)
        birth = date(rng.randint(1950, 2004), rng.randint(1, 12), rng.randint(1, 28)).isoformat()
        place, _city = rng.choice(PLACES)
        cf = cf_for(surname, name, birth, sex, place)
        phone = None if i in NO_PHONE else f"+393{rng.randint(20, 99)}{rng.randint(1000000, 9999999)}"
        if cf in seen or (name, surname) in {(p["name"], p["surname"]) for p in people}:
            continue
        seen.add(cf)
        people.append({"name": name, "surname": surname, "sex": sex, "birth": birth, "place": place,
                       "cf": cf, "phone": phone})
    return people


def _book_first_free(conn, pid, day, times, tag):
    import appointments
    for offset in range(0, 21):
        d = day + timedelta(days=offset)
        for hhmm in times:
            try:
                return appointments.book(conn, pid, "dentist", f"{d.isoformat()}T{hhmm}", 30, note=tag)
            except Exception:
                continue
    raise RuntimeError(f"no free slot for {tag}")


def seed(conn, seed=22, anchor="2026-10-05"):
    import appointments
    import consent
    import ledger
    import patient_id
    conn.executescript(SCHEMA)
    created = {"patients": 0, "visits": 0, "appointments": 0, "consents": 0}
    start = date.fromisoformat(anchor)
    for i, p in enumerate(cohort(seed)):
        full = f"{p['name']} {p['surname']}"
        pid = patient_id.resolve(conn, p["cf"])
        if pid is None:
            pid = patient_id.seed_patient(conn, p["cf"], full, p["phone"])
            created["patients"] += 1
        conn.execute("INSERT OR IGNORE INTO demo_identities (patient_id, seed, created_at) VALUES (?, ?, ?)",
                     (pid, seed, clinic_time.stamp()))

        procedures, text, amount = PROCEDURES[i % len(PROCEDURES)]
        visit_day = (start - timedelta(days=30 + 7 * i)).isoformat()
        src = f"demo-seed/{p['cf']}/v1.json"
        cur = conn.execute("INSERT OR IGNORE INTO visits (patient_id, visit_date, procedures, clinical_notes,"
                           " next_appointment, source_path) VALUES (?, ?, ?, ?, ?, ?)",
                           (pid, visit_day, json.dumps(procedures), text, "controllo tra 6 mesi", src))
        if cur.rowcount:
            created["visits"] += 1
            visit_id = cur.lastrowid
            conn.execute("INSERT INTO invoices (patient_id, visit_id, line_index, amount, amount_cents, description)"
                         " VALUES (?, ?, 0, ?, ?, ?)", (pid, visit_id, amount, int(round(amount * 100)), text))
            ledger.ensure_invoice(conn, pid, visit_id)

        def tagged(k):
            return conn.execute("SELECT 1 FROM appointments WHERE note = ?", (f"demo-seed:{p['cf']}:{k}",)).fetchone()

        if not tagged(0):
            _book_first_free(conn, pid, start + timedelta(days=i), ["09:30", "10:30", "11:30", "15:00"],
                             f"demo-seed:{p['cf']}:0")
            created["appointments"] += 1
        if i % 3 == 1 and not tagged(1):
            aid = _book_first_free(conn, pid, start + timedelta(days=i + 7), ["16:00", "17:00"],
                                   f"demo-seed:{p['cf']}:1")
            appointments.cancel(conn, aid)
            created["appointments"] += 1
        if i % 4 == 2 and not conn.execute("SELECT 1 FROM appointments WHERE patient_id = ? AND status = 'requested'",
                                           (pid,)).fetchone():
            appointments.request(conn, pid, (start + timedelta(days=i + 14)).isoformat(), "morning",
                                 "demo-seed request")
            created["appointments"] += 1
        if consent.current(conn, pid, "messaging") is None:
            consent.record(conn, pid, "messaging", i % 2 == 0, "demo-seed", "system", note="demo cohort")
            created["consents"] += 1
    conn.commit()
    return {"seed": seed, "anchor": anchor, "created": created}


def counts(conn):
    q = lambda sql: conn.execute(sql).fetchone()[0]     # noqa: E731
    return {"patients": q("SELECT COUNT(*) FROM patients"), "visits": q("SELECT COUNT(*) FROM visits"),
            "appointments": q("SELECT COUNT(*) FROM appointments"), "consents": q("SELECT COUNT(*) FROM consent_records"),
            "demo_identities": q("SELECT COUNT(*) FROM demo_identities")}


def main(argv):
    import storage
    arg = lambda k, d: argv[argv.index(k) + 1] if k in argv else d     # noqa: E731
    conn = storage.init_db(arg("--db", "db/clinic.sqlite"))
    try:
        before = counts(conn)
        out = seed(conn, seed=int(arg("--seed", 22)), anchor=arg("--anchor", "2026-10-05"))
        print(json.dumps({**out, "before": before, "after": counts(conn)}, indent=1))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
