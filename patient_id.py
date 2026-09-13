"""The patient's durable technical identity.

WHY THIS EXISTS. The codice fiscale was the primary key, every foreign key, the
Chroma metadata key, the `sorted/<CF>/` directory name and the URL. That is one
value doing seven jobs, and it is also personal data: a codice fiscale encodes
a surname, a first name, a birth date and a birthplace. Using it as the storage
key means identity cannot be corrected without rewriting every relation, and it
means the key handed to a payment provider or a scheduling integration is
itself a disclosure.

THE SURROGATE IS OPAQUE AND RANDOM, NOT AN AUTOINCREMENT INTEGER. An integer
would carry no personal data either, but it is enumerable and it leaks ordering
and volume - and this is explicitly the identifier that may be given to a third
party. `pid_` + 16 hex is opaque, URL-safe, filesystem-safe, and stable for the
life of the record.

THE CODICE FISCALE REMAINS, as a validated and MUTABLE attribute. It is still
how a person is looked up, still what a dentist's note contains, and still
unique. What it is no longer is identity: correcting a mistyped codice fiscale
must not move a single visit, invoice or appointment.
"""

import re
import secrets
import sys

PREFIX = "pid_"
PATTERN = re.compile(r"^pid_[0-9a-f]{16}$")


def new_id():
    return PREFIX + secrets.token_hex(8)


def is_valid(value):
    return isinstance(value, str) and bool(PATTERN.match(value))


def resolve(conn, key):
    """A patient_id, a live codice fiscale, or a folded one -> patient_id.

    ONE RESOLVER. Every surface takes some of these - a URL carries whichever
    the link was made with, a note carries a codice fiscale, an old audit row
    carries a codice fiscale that has since been merged away. A caller that
    writes its own version of this is a caller that will forget one of the
    three, and the forgotten one is always the folded CF.

    Returns None when the key names nobody.
    """
    if not key:
        return None
    if is_valid(key):
        row = conn.execute(
            "SELECT patient_id FROM patients WHERE patient_id = ?", (key,)).fetchone()
        return row["patient_id"] if row else None

    row = conn.execute(
        "SELECT patient_id FROM patients WHERE codice_fiscale = ?", (key,)).fetchone()
    if row:
        return row["patient_id"]

    # a folded codice fiscale. P04 kept the mapping precisely so an old link
    # still leads somewhere true rather than 404ing.
    row = conn.execute(
        "SELECT target_patient_id FROM patient_merges WHERE source_cf = ?", (key,)).fetchone()
    if row and row["target_patient_id"]:
        return resolve(conn, row["target_patient_id"])
    return None


def selftest():
    import tempfile
    from pathlib import Path

    import storage

    # 1. the shape: opaque, no personal data, and not enumerable
    a, b = new_id(), new_id()
    assert is_valid(a) and is_valid(b), "1: a generated id must be valid"
    assert a != b, "1: two ids must differ"
    assert a.startswith(PREFIX) and len(a) == len(PREFIX) + 16, "1: pid_ + 16 hex"
    for bad in ("", None, "pid_", "pid_xyz", "RSSM800010150100", "pid_" + "g" * 16,
                "PID_0123456789abcdef", 42):
        assert not is_valid(bad), f"1: {bad!r} must not validate"
    # a thousand ids with no collision - not proof of uniqueness, but a shape
    # error that produced a constant would show here
    assert len({new_id() for _ in range(1000)}) == 1000, "1: ids must not repeat"

    import migrate_pid

    with tempfile.TemporaryDirectory() as tmp:
        # the v2 shape comes from the real migration, never from DDL written in
        # a test file. audit_log's ip and reason columns broke the fast suite
        # twice because three test files built that table by hand.
        conn = storage.init_db(str(Path(tmp) / "t.sqlite"))
        conn.execute("INSERT INTO patients (codice_fiscale, patient_name, phone)"
                     " VALUES ('RSSM800010150100', 'Mario Rossi', NULL)")
        conn.commit()
        migrate_pid.ensure_ops_table(conn)
        migrate_pid.sqlite_step(conn)
        pid = conn.execute("SELECT patient_id FROM patients").fetchone()["patient_id"]

        # 2. resolve by the surrogate itself
        assert resolve(conn, pid) == pid, "2: a patient_id resolves to itself"

        # 3. and by the codice fiscale, which is still how a person is found
        assert resolve(conn, "RSSM800010150100") == pid, "3: a live CF resolves"

        # 4. a key naming nobody resolves to nothing - it must not raise, and it
        # must not fall through to some other patient
        assert resolve(conn, "ZZZZ999999999999") is None, "4: an unknown CF is nobody"
        assert resolve(conn, new_id()) is None, "4: an unissued patient_id is nobody"
        assert resolve(conn, None) is None and resolve(conn, "") is None, "4: empty is nobody"

        # 5. A FOLDED CODICE FISCALE STILL RESOLVES. this is the P04 promise
        # carried forward: an old link, an old audit row and an old file path
        # all still name the merged-away CF, and 404 would make the merge a
        # trace-free delete from every surface a person actually uses.
        gone = "RSSP850010150900"
        conn.execute(
            "INSERT INTO patient_merges (source_cf, target_cf, target_patient_id, merged_at,"
            " merged_by, source_row, moved) VALUES (?, 'RSSM800010150100', ?, '', '', '{}', '{}')",
            (gone, pid))
        conn.commit()
        assert resolve(conn, gone) == pid, "5: a folded CF resolves to the survivor"

        # 6. CHANGING THE CODICE FISCALE DOES NOT CHANGE IDENTITY. correcting a
        # mistyped CF used to mean rewriting every relation; that is the whole
        # reason this module exists.
        conn.execute(
            "UPDATE patients SET codice_fiscale = 'RSSM800010150101' WHERE patient_id = ?",
            (pid,))
        conn.commit()
        assert resolve(conn, "RSSM800010150101") == pid, "6: the new CF resolves to the same id"
        assert resolve(conn, pid) == pid, "6: and the identity itself never moved"
        assert resolve(conn, "RSSM800010150100") is None, \
            "6: the old CF no longer names a live patient"
        assert resolve(conn, gone) == pid, \
            "6: while the FOLDED CF still resolves - it is a mapping, not a lookup"

    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    print("usage: python patient_id.py --selftest")


if __name__ == "__main__":
    main()
