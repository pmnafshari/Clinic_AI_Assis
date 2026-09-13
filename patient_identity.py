"""Duplicate patient review, and the merge that follows a human deciding.

THE FAILURE THIS MODULE EXISTS TO PREVENT IS THE WRONG MERGE. Two people
collapsed into one record is not recoverable by apology: one patient's clinical
history is now attached to another's name, and the clinic cannot tell which
half is whose. Everything here is shaped by that:

  * DETECTION NEVER MERGES. candidates() returns pairs and the reasons they
    look alike. No score anywhere triggers an automatic merge, and `strong`
    means "a human should look at this", not "this is the same person". The
    dev fixtures carry two Paola Rossi with different codici fiscali precisely
    because they may well be two different people.
  * A DISMISSAL IS A DECISION AND IT IS KEPT. "These two are different people"
    is an answer, and re-asking it every week trains staff to click through
    the question.
  * A MERGED CODICE FISCALE STAYS RESOLVABLE. patient_merges keeps the mapping
    and the whole source row, so an old link, an old audit row and an old file
    path still lead somewhere true. That is what makes this not a trace-free
    delete, which P04.03 forbids.

The codice fiscale is the primary key of `patients` and the foreign key of
every relation, so a merge is a repoint, not a rename. It also keys the Chroma
chunk ids AND the chunk metadata that scopes a patient's retrieval, and the
`sorted/<CF>/` directory on disk - so a merge that only touches SQLite leaves
the survivor's own notes unretrievable and leaves index chunks scoped to an
identity that no longer exists. All three stores move together or the merge is
not done.
"""

import difflib
import json
import re
import sys
import unicodedata
from datetime import datetime

from auth import authorize, log_audit

SCHEMA = """
CREATE TABLE IF NOT EXISTS patient_merges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_cf TEXT NOT NULL UNIQUE,
    target_cf TEXT NOT NULL REFERENCES patients(codice_fiscale),
    merged_at TEXT NOT NULL,
    merged_by TEXT NOT NULL,
    source_row TEXT NOT NULL,
    moved TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS patient_duplicate_dismissals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cf_a TEXT NOT NULL,
    cf_b TEXT NOT NULL,
    dismissed_at TEXT NOT NULL,
    dismissed_by TEXT NOT NULL,
    reason TEXT,
    UNIQUE(cf_a, cf_b)
);
"""

# how alike two names have to look before the pair is worth a human's time.
# 0.88 keeps "Paola Rossi"/"Paolo Rossi" in and "Rossi"/"Bianchi" out. it is a
# threshold for ASKING, never for acting - see the module docstring.
NAME_RATIO = 0.88

WEAK, REVIEW, STRONG = "weak", "review", "strong"


def normalize_name(value):
    # accents folded, case folded, punctuation dropped, whitespace collapsed.
    # "D'Angelo" and "d angelo" are the same name written twice by two people
    # in a hurry, and the clinic has no canonical spelling.
    if not value:
        return ""
    flat = unicodedata.normalize("NFKD", value)
    flat = "".join(c for c in flat if not unicodedata.combining(c))
    flat = re.sub(r"[^a-z0-9\s]", " ", flat.casefold())
    return " ".join(flat.split())


def normalize_phone(value):
    # digits only. "+39 333 999 0099" and "3339990099" are one number.
    return re.sub(r"\D", "", value or "")


def _positional_diff(a, b):
    return sum(1 for x, y in zip(a, b) if x != y)


def _is_transposition(a, b):
    # exactly one adjacent swap apart. RSPS...  vs RSSP... is how a codice
    # fiscale gets typed wrong, and it is the fixture case in the dev db.
    if len(a) != len(b) or a == b:
        return False
    diff = [i for i, (x, y) in enumerate(zip(a, b)) if x != y]
    return (len(diff) == 2 and diff[1] == diff[0] + 1
            and a[diff[0]] == b[diff[1]] and a[diff[1]] == b[diff[0]])


def signals(a, b):
    """Independent facts about two patient rows. Each one is weak alone.

    Returned as a dict rather than a number so the review screen can say WHY
    in words. A staff member deciding whether two records are one person needs
    the reasons, not a score they have no way to audit.
    """
    name_a, name_b = normalize_name(a["patient_name"]), normalize_name(b["patient_name"])
    phone_a, phone_b = normalize_phone(a["phone"]), normalize_phone(b["phone"])
    cf_a, cf_b = a["codice_fiscale"], b["codice_fiscale"]
    same_length = len(cf_a) == len(cf_b)
    return {
        "name_exact": bool(name_a) and name_a == name_b,
        "name_close": bool(name_a) and name_a != name_b
                      and difflib.SequenceMatcher(None, name_a, name_b).ratio() >= NAME_RATIO,
        "phone_exact": bool(phone_a) and phone_a == phone_b,
        "cf_close": same_length and 0 < _positional_diff(cf_a, cf_b) <= 2,
        "cf_transposed": _is_transposition(cf_a, cf_b),
        "cf_same_digits": same_length
                          and re.sub(r"\D", "", cf_a) == re.sub(r"\D", "", cf_b)
                          and cf_a != cf_b,
    }


def strength(sig):
    """weak / review / strong. STRONG STILL MEANS A HUMAN DECIDES.

    Deliberately not a probability. A number invites a threshold, and a
    threshold invites somebody automating the merge behind it.
    """
    name = sig["name_exact"] or sig["name_close"]
    cf = sig["cf_close"] or sig["cf_transposed"] or sig["cf_same_digits"]
    if name and (cf or sig["phone_exact"]):
        return STRONG
    if name or (cf and sig["phone_exact"]):
        return REVIEW
    return WEAK


def _reasons(sig):
    # what the review screen prints. words, not flags.
    out = []
    if sig["name_exact"]:
        out.append("the same name")
    elif sig["name_close"]:
        out.append("nearly the same name")
    if sig["phone_exact"]:
        out.append("the same phone number")
    if sig["cf_transposed"]:
        out.append("two letters of the codice fiscale swapped")
    elif sig["cf_same_digits"]:
        out.append("the same digits in the codice fiscale, different letters")
    elif sig["cf_close"]:
        out.append("a codice fiscale differing in at most two places")
    return out


def dismissed_pairs(conn):
    return {(r["cf_a"], r["cf_b"]) for r in
            conn.execute("SELECT cf_a, cf_b FROM patient_duplicate_dismissals")}


def merged_sources(conn):
    return {r["source_cf"] for r in conn.execute("SELECT source_cf FROM patient_merges")}


def candidates(conn, limit=50):
    """Pairs worth a human's attention, strongest first. Reads only.

    O(n^2) over the patient table on purpose: a single-clinic patient list is
    thousands of rows at most, and a blocking key that skipped a real duplicate
    would defeat the whole point of the screen.
    """
    rows = conn.execute(
        "SELECT codice_fiscale, patient_name, phone FROM patients ORDER BY codice_fiscale"
    ).fetchall()
    skip = dismissed_pairs(conn)
    gone = merged_sources(conn)
    order = {STRONG: 0, REVIEW: 1, WEAK: 2}
    found = []
    for i, a in enumerate(rows):
        for b in rows[i + 1:]:
            pair = tuple(sorted((a["codice_fiscale"], b["codice_fiscale"])))
            if pair in skip or pair[0] in gone or pair[1] in gone:
                continue
            sig = signals(a, b)
            level = strength(sig)
            if level == WEAK:
                continue
            found.append({
                "a": dict(a),
                "b": dict(b),
                "signals": sig,
                "strength": level,
                "reasons": _reasons(sig),
            })
    found.sort(key=lambda c: (order[c["strength"]], c["a"]["codice_fiscale"]))
    return found[:limit]


def dismiss(conn, cf_a, cf_b, actor, actor_role, reason=None):
    """Record that a human looked at a pair and said they are different people.

    Gated like the merge: deciding two records are NOT the same person is the
    other half of the same judgement, and a wrong dismissal hides a real
    duplicate from everyone who looks after it.
    """
    if not authorize(actor_role, "manage_users"):
        log_audit(conn, actor, actor_role, "dismiss_duplicate", f"{cf_a}|{cf_b}", allowed=0)
        return False, f"not permitted: {actor_role} may not manage_users"
    a, b = sorted((cf_a, cf_b))
    if a == b:
        return False, "a record is not a duplicate of itself"
    conn.execute(
        "INSERT OR IGNORE INTO patient_duplicate_dismissals"
        " (cf_a, cf_b, dismissed_at, dismissed_by, reason) VALUES (?, ?, ?, ?, ?)",
        (a, b, datetime.now().isoformat(), actor, (reason or "").strip() or None),
    )
    conn.commit()
    log_audit(conn, actor, actor_role, "dismiss_duplicate", f"{a}|{b}", allowed=1)
    return True, "Recorded as two different people."


def selftest():
    import sqlite3
    import tempfile
    from pathlib import Path

    import storage

    with tempfile.TemporaryDirectory() as tmp:
        conn = storage.init_db(str(Path(tmp) / "t.sqlite"))

        # 1. the tables come from init_db, not from this test. the appointments
        # table went in this way and the audit_log ip/reason columns broke the
        # fast suite twice by being hand-rolled in test files.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(patient_merges)")}
        assert "source_cf" in cols and "source_row" in cols, \
            "1: init_db must create patient_merges"
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(patient_duplicate_dismissals)")}
        assert "cf_a" in cols, "1: init_db must create patient_duplicate_dismissals"

        # 2. name normalisation - the clinic has no canonical spelling
        assert normalize_name("Paola Rossi") == normalize_name("  paola   ROSSI ")
        assert normalize_name("D'Angelo") == normalize_name("d angelo")
        assert normalize_name("Nicolò") == normalize_name("Nicolo")
        assert normalize_name(None) == "", "2: a missing name is empty, not a crash"
        assert normalize_phone("+39 333 999 0099") == "393339990099"
        assert normalize_phone(None) == ""

        # 3. transposition is the way a codice fiscale gets typed wrong, and it
        # is the real pair sitting in the dev database
        assert _is_transposition("RSPS850010150900", "RSSP850010150900"), \
            "3: an adjacent swap must be recognised"
        assert not _is_transposition("RSPS850010150900", "RSPS850010150900"), \
            "3: a value is not a transposition of itself"
        assert not _is_transposition("AAAA1", "BBBB2"), "3: unrelated values are not transpositions"

        def row(cf, name, phone=None):
            return {"codice_fiscale": cf, "patient_name": name, "phone": phone}

        # 4. THE CENTRAL CASE. same name, different codice fiscale. this is
        # strong - and strong still means a human decides. the assertion that
        # matters is the one in check 7: nothing was written.
        sig = signals(row("RSPS850010150900", "Paola Rossi"),
                      row("RSSP850010150900", "paola rossi"))
        assert sig["name_exact"], "4: the names match once normalised"
        assert sig["cf_transposed"] and sig["cf_same_digits"], "4: and the CFs are a swap apart"
        assert strength(sig) == STRONG, "4: name + CF proximity is worth a human's time"

        # 5. a shared name ALONE is not strong. Rossi is a common surname, and
        # treating two unrelated people with one name as a likely duplicate is
        # how a wrong merge gets proposed in the first place.
        sig5 = signals(row("AAAA000000000001", "Mario Rossi"),
                       row("ZZZZ999999999999", "Mario Rossi"))
        assert sig5["name_exact"] and not sig5["cf_close"], "5: same name, unrelated CFs"
        assert strength(sig5) == REVIEW, "5: a shared name alone is review, never strong"

        # 6. and two different people are not a candidate at all
        assert strength(signals(row("AAAA000000000001", "Mario Rossi"),
                                row("ZZZZ999999999999", "Giulia Bianchi"))) == WEAK, \
            "6: unrelated records must not reach the review screen"

        # 7. DETECTION WRITES NOTHING. the whole module is a suggestion until a
        # human acts; a detector with a side effect is a merge engine.
        conn.execute("INSERT INTO patients VALUES (?,?,?)",
                     ("RSPS850010150900", "Paola Rossi", None))
        conn.execute("INSERT INTO patients VALUES (?,?,?)",
                     ("RSSP850010150900", "paola rossi", "555 0000"))
        conn.execute("INSERT INTO patients VALUES (?,?,?)",
                     ("BNCG800010150100", "Giulia Bianchi", None))
        conn.commit()
        before = conn.execute("SELECT COUNT(*) c FROM patients").fetchone()["c"]
        found = candidates(conn)
        after = conn.execute("SELECT COUNT(*) c FROM patients").fetchone()["c"]
        assert before == after == 3, "7: finding duplicates must not change any patient"
        assert conn.execute("SELECT COUNT(*) c FROM patient_merges").fetchone()["c"] == 0, \
            "7: and must not merge anything"

        # 8. the pair is surfaced, the unrelated record is not, and the reasons
        # are readable words rather than a score nobody can audit
        assert len(found) == 1, f"8: expected exactly one candidate pair, got {len(found)}"
        pair = found[0]
        assert {pair["a"]["codice_fiscale"], pair["b"]["codice_fiscale"]} == \
            {"RSPS850010150900", "RSSP850010150900"}, "8: the Paola pair is the candidate"
        assert pair["strength"] == STRONG
        assert "the same name" in pair["reasons"], "8: the screen must say why, in words"
        assert any("swapped" in r for r in pair["reasons"]), "8: including the CF signal"

        # 9. a dismissal is a decision and it is kept - the pair does not come
        # back next week to be clicked through again
        ok, msg = dismiss(conn, "RSSP850010150900", "RSPS850010150900",
                          "anadmin", "admin", "different people, checked the records")
        assert ok, f"9: an admin may dismiss a pair - {msg}"
        assert candidates(conn) == [], "9: a dismissed pair leaves the review screen"
        # order must not matter, or the same pair comes back reversed
        assert dismissed_pairs(conn) == {("RSPS850010150900", "RSSP850010150900")}, \
            "9: the pair is stored in a stable order"

        # 10. dismissing is the same judgement as merging and needs the same
        # capability: a wrong dismissal hides a real duplicate from everyone.
        ok10, msg10 = dismiss(conn, "AAAA000000000001", "ZZZZ999999999999",
                              "adentist", "dentist")
        assert not ok10 and "not permitted" in msg10, "10: a dentist must not dismiss"
        denied = conn.execute(
            "SELECT * FROM audit_log WHERE action = 'dismiss_duplicate' AND allowed = 0"
        ).fetchall()
        assert denied and denied[0]["username"] == "adentist", \
            "10: and the refusal is audited, naming who was refused"
        assert conn.execute(
            "SELECT COUNT(*) c FROM patient_duplicate_dismissals").fetchone()["c"] == 1, \
            "10: a refused dismissal writes nothing"

    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    print("usage: python patient_identity.py --selftest")


if __name__ == "__main__":
    main()
