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
every relation, so a merge is a repoint, not a rename. It also appears in the
Chroma chunk metadata, which staff Q&A reads to attribute an answer to a
patient - so a merge that only touches SQLite leaves the survivor's own notes
cited under a name that no longer exists. The index is repointed with the rows.

Files are NOT moved. `sorted/<CF>/` is a storage location, not a claim about
identity, and there is no transaction spanning SQLite and the filesystem; the
files view resolves through merged_sources_of() instead. See .planning/plans/
P04.md section 4, plan 2, for why that is the safer half of the trade.
"""

import patient_id as _pidmod
import difflib
import json
import re
import shutil
import sqlite3
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

from auth import authorize, log_audit

# patient_merges and patient_duplicate_dismissals moved to
# migrate_pid.TABLE_BODIES in Phase 51: they are keyed on patient_id now, and
# `target_cf` lost its foreign key onto patients(codice_fiscale) - that key was
# what kept the codice fiscale a relationship value and made correcting a
# mistyped one impossible.
SCHEMA = ""

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
    return {(r["patient_id_a"], r["patient_id_b"]) for r in
            conn.execute("SELECT patient_id_a, patient_id_b FROM patient_duplicate_dismissals")}


def merged_sources(conn):
    return {r["source_cf"] for r in conn.execute("SELECT source_cf FROM patient_merges")}


def candidates(conn, limit=50):
    """Pairs worth a human's attention, strongest first. Reads only.

    O(n^2) over the patient table on purpose: a single-clinic patient list is
    thousands of rows at most, and a blocking key that skipped a real duplicate
    would defeat the whole point of the screen.
    """
    rows = conn.execute(
        "SELECT patient_id, codice_fiscale, patient_name, phone FROM patients"
        " ORDER BY codice_fiscale").fetchall()
    skip = dismissed_pairs(conn)
    gone = merged_sources(conn)
    order = {STRONG: 0, REVIEW: 1, WEAK: 2}
    found = []
    for i, a in enumerate(rows):
        for b in rows[i + 1:]:
            # the pair is identified by SURROGATE now, so a dismissal survives
            # a codice fiscale being corrected on either side
            pair = tuple(sorted((a["patient_id"], b["patient_id"])))
            if pair in skip or a["codice_fiscale"] in gone or b["codice_fiscale"] in gone:
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
    import patient_id as _pid

    pid_a, pid_b = _pid.resolve(conn, cf_a), _pid.resolve(conn, cf_b)
    if pid_a is None or pid_b is None:
        return False, "both records must exist"
    if pid_a == pid_b:
        return False, "a record is not a duplicate of itself"
    a, b = sorted((pid_a, pid_b))
    cfs = dict(conn.execute(
        "SELECT patient_id, codice_fiscale FROM patients WHERE patient_id IN (?, ?)",
        (a, b)).fetchall())
    conn.execute(
        "INSERT OR IGNORE INTO patient_duplicate_dismissals"
        " (patient_id_a, patient_id_b, cf_a, cf_b, dismissed_at, dismissed_by, reason)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (a, b, cfs.get(a, ""), cfs.get(b, ""),
         datetime.now().isoformat(), actor, (reason or "").strip() or None),
    )
    conn.commit()
    log_audit(conn, actor, actor_role, "dismiss_duplicate", f"{a}|{b}", allowed=1)
    return True, "Recorded as two different people."


# every relation keyed by the codice fiscale that a merge has to repoint.
# patient_credentials is deliberately NOT here - it is UNIQUE on the CF, so a
# repoint collides whenever both sides hold a PIN, and a merged-away identity
# must not keep an independent way to sign in. it is revoked instead.
MERGE_RELATIONS = ("visits", "invoices", "appointments", "patient_sessions")

# Phase 51: everything below is keyed on patient_id. The codice fiscale is kept
# on `patient_merges.source_cf` because resolving an OLD one is that table's
# whole job, but nothing here relates rows by it any more.


def merge_target(conn, cf):
    """-> the CF this one was merged into, or None. Always one hop.

    An old link, an old audit row and an old file path all still name the
    source. They must lead somewhere true rather than 404, which is what makes
    this not a trace-free delete.

    One hop is enough because merge() FLATTENS: when B is merged into C,
    everything that had been merged into B is repointed at C in the same
    transaction. That is not only a convenience - `patient_merges.target_cf`
    REFERENCES patients(codice_fiscale) with foreign keys ON, so leaving a row
    pointing at B would make deleting B's patients row fail outright.
    """
    row = conn.execute(
        "SELECT target_cf FROM patient_merges WHERE source_cf = ?", (cf,)).fetchone()
    return row["target_cf"] if row else None


def merge_target_pid(conn, cf):
    """The surrogate a folded codice fiscale now belongs to."""
    row = conn.execute(
        "SELECT target_patient_id FROM patient_merges WHERE source_cf = ?", (cf,)).fetchone()
    return row["target_patient_id"] if row else None


def merged_sources_of(conn, target):
    """Every codice fiscale folded into this patient. Flat, for the reason above.

    Takes a patient_id or a codice fiscale. Kept after Phase 51 because old
    audit rows, old links and old file paths still name those codici fiscali -
    but it is NO LONGER how the files are found. The merge moves them now; see
    merge().
    """
    import patient_id as _pid

    pid = _pid.resolve(conn, target)
    if pid is None:
        return []
    return [r["source_cf"] for r in conn.execute(
        "SELECT source_cf FROM patient_merges WHERE target_patient_id = ? ORDER BY merged_at",
        (pid,))]


def merge(conn, source_cf, target_cf, actor, actor_role, collection=None,
          sorted_root=None):
    """Fold source into target. THE DESTRUCTIVE ONE. Returns (ok, message).

    Only ever called from a human confirmation naming both sides - never from
    candidates(), never from a score.

    Order is deliberate. SQLite commits first, in one transaction, so a failure
    part-way leaves nothing moved. The index is repointed after, and if that
    fails the merge is already recorded and the repoint is re-runnable against
    the mapping. The reverse order would leave chunks pointing at a survivor
    whose relations had not moved.

    THE FILES MOVE TOO, since Phase 51. They did not before, and that was
    MERGE-1: `agent.py` rebuilt a note path from the survivor's identity while
    the file sat under the folded one's directory, so an edit to a merged-in
    visit failed. Now the folded patient's directory is merged into the
    survivor's and `visits.source_path` is rewritten with it. The move cannot
    share a transaction with SQLite, so it is recorded in `migration_ops` the
    same way the Phase 51 migration records its own work: if it cannot be
    finished the op stays pending, visible and repairable, and nothing on disk
    is deleted.
    """
    if not authorize(actor_role, "manage_users"):
        log_audit(conn, actor, actor_role, "merge_patient",
                  f"{source_cf}->{target_cf}", allowed=0)
        return False, f"not permitted: {actor_role} may not manage_users"

    source_cf = (source_cf or "").strip()
    target_cf = (target_cf or "").strip()
    if not source_cf or not target_cf:
        return False, "both records must be named"
    if source_cf == target_cf:
        return False, "a record cannot be merged into itself"

    # asked BEFORE "does this patient exist", because a merged source no
    # longer has a patients row - ask the other way round and this branch is
    # unreachable and the operator retrying a merge is told the patient does
    # not exist, which is both unhelpful and untrue.
    already = conn.execute(
        "SELECT target_cf FROM patient_merges WHERE source_cf = ?", (source_cf,)).fetchone()
    if already:
        return False, f"{source_cf} has already been merged into {already['target_cf']}"

    source = conn.execute(
        "SELECT * FROM patients WHERE codice_fiscale = ?", (source_cf,)).fetchone()
    target = conn.execute(
        "SELECT * FROM patients WHERE codice_fiscale = ?", (target_cf,)).fetchone()
    if source is None:
        return False, f"no patient with codice fiscale {source_cf}"
    if target is None:
        return False, f"no patient with codice fiscale {target_cf}"
    source_pid, target_pid = source["patient_id"], target["patient_id"]
    moved = {}
    try:
        # one transaction over every relation. sqlite3 opens one implicitly on
        # the first write and holds it until commit, so a raise anywhere below
        # rolls the whole thing back and nothing has moved.
        for table in MERGE_RELATIONS:
            cur = conn.execute(
                f"UPDATE {table} SET patient_id = ? WHERE patient_id = ?",
                (target_pid, source_pid))
            moved[table] = cur.rowcount
        cur = conn.execute(
            "UPDATE patient_credentials SET active = 0 WHERE patient_id = ?", (source_pid,))
        moved["credentials_revoked"] = cur.rowcount
        # FLATTEN. anything already merged into the source is repointed at the
        # new survivor, in this same transaction. staff merge A into B and only
        # later find B is also a duplicate of C, and A must not be stranded.
        # the original target is kept on each affected row so the history is
        # still readable - the mapping is rewritten, not erased.
        rechained = conn.execute(
            "SELECT source_cf, moved FROM patient_merges WHERE target_patient_id = ?",
            (source_pid,)).fetchall()
        for old_row in rechained:
            history = json.loads(old_row["moved"])
            history.setdefault("original_target", source_cf)
            conn.execute(
                "UPDATE patient_merges SET target_cf = ?, target_patient_id = ?, moved = ?"
                " WHERE source_cf = ?",
                (target_cf, target_pid, json.dumps(history), old_row["source_cf"]))
        moved["rechained"] = [r["source_cf"] for r in rechained]

        conn.execute(
            "INSERT INTO patient_merges (source_cf, target_cf, target_patient_id, merged_at,"
            " merged_by, source_row, moved) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (source_cf, target_cf, target_pid, datetime.now().isoformat(), actor,
             json.dumps(dict(source)), json.dumps(moved)))
        # the credential row still references the source patient, so it has to
        # go before the patients row does - the FK is ON
        conn.execute("DELETE FROM patient_credentials WHERE patient_id = ?", (source_pid,))
        conn.execute("DELETE FROM patients WHERE patient_id = ?", (source_pid,))
        # the two stores that cannot join this transaction are enqueued inside
        # it, so an interruption after the commit still leaves the work
        # recorded rather than forgotten (the Phase 51 pattern)
        import migrate_pid
        conn.executescript(migrate_pid.OPS_SCHEMA)
        conn.execute(
            "INSERT OR REPLACE INTO migration_ops (migration, step, subject, state, payload,"
            " updated_at) VALUES (?, ?, ?, 'pending', ?, ?)",
            ("merge_files", migrate_pid.STEP_FS, target_pid, source_pid,
             datetime.now().isoformat()))
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    # the index step is recorded EITHER WAY. a skip that leaves no trace is a
    # skip nobody can find later: the chunks still carry the folded patient's
    # name, and the only way to know which merges need re-pointing is a record
    # that says so. `index_pending` is what a repair pass looks for.
    if collection is not None:
        moved["index_chunks"] = repoint_index(collection, source_pid, target_pid,
                                              target["patient_name"])
    else:
        moved["index_chunks"] = None
        moved["index_pending"] = True
    conn.execute("UPDATE patient_merges SET moved = ? WHERE source_cf = ?",
                 (json.dumps(moved), source_cf))
    conn.commit()

    # same shape as the index step: do it now if we can, and if we cannot the
    # op stays pending and visible rather than silently not happening
    if sorted_root is not None:
        moved["files"] = move_merged_files(conn, sorted_root)
        conn.execute("UPDATE patient_merges SET moved = ? WHERE source_cf = ?",
                     (json.dumps(moved), source_cf))
        conn.commit()

    log_audit(conn, actor, actor_role, "merge_patient",
              f"{source_cf}->{target_cf}", allowed=1)
    return True, f"Merged into {target['patient_name']}."


def move_merged_files(conn, sorted_root="sorted"):
    """Fold each merged patient's directory into the survivor's. THE MERGE-1 FIX.

    Before Phase 51 a merge left the folded patient's files where they were and
    the file LIST resolved through the mapping - but `agent.py` rebuilt an edit
    path from the surviving identity, so editing a merged-in visit looked for a
    file that was not there. It failed closed rather than writing to the wrong
    patient, which is why it was a defect to schedule rather than an emergency,
    but it was still a real inconsistency.

    Now the files move with the rows. This cannot share a transaction with the
    SQLite half, so each move is a durable op: pending until it completes,
    visible if it does not, and safe to re-run. Nothing is deleted - a name
    collision is resolved by renaming the incoming file, the way
    sort_files._move already does.
    """
    import migrate_pid

    sorted_root = Path(sorted_root)
    done = []
    try:
        ops = conn.execute(
            "SELECT subject, payload FROM migration_ops"
            " WHERE migration = 'merge_files' AND state = 'pending' ORDER BY id").fetchall()
    except sqlite3.OperationalError:
        return done

    for op in ops:
        target_pid, source_pid = op["subject"], op["payload"]
        src, dest = sorted_root / source_pid, sorted_root / target_pid
        try:
            if not source_pid:
                migrate_pid._mark(conn, migrate_pid.STEP_FS, target_pid, migrate_pid.FAILED,
                                  "no source recorded", )
                continue
            if src.exists():
                dest.mkdir(parents=True, exist_ok=True)
                for item in sorted(src.rglob("*")):
                    if not item.is_file():
                        continue
                    rel = item.relative_to(src)
                    landing = dest / rel
                    landing.parent.mkdir(parents=True, exist_ok=True)
                    n = 1
                    while landing.exists():
                        landing = landing.parent / f"{rel.stem}_{n}{rel.suffix}"
                        n += 1
                    shutil.move(str(item), str(landing))
                    conn.execute(
                        "UPDATE visits SET source_path = ? WHERE source_path = ?",
                        (str(Path(*landing.parts[-4:])) if len(landing.parts) >= 4
                         else str(landing),
                         str(Path(*item.parts[-4:])) if len(item.parts) >= 4 else str(item)))
                shutil.rmtree(src, ignore_errors=True)
            # whatever the paths looked like, make sure no row still names the
            # folded directory
            conn.execute(
                "UPDATE visits SET source_path = replace(source_path, ?, ?)"
                " WHERE source_path LIKE ?",
                (f"/{source_pid}/", f"/{target_pid}/", f"%/{source_pid}/%"))
            conn.execute(
                "UPDATE migration_ops SET state = 'done', detail = ?, attempts = attempts + 1,"
                " updated_at = ? WHERE migration = 'merge_files' AND subject = ?",
                (f"{source_pid} -> {target_pid}", datetime.now().isoformat(), target_pid))
            conn.commit()
            done.append({"from": source_pid, "into": target_pid})
        except Exception as e:
            conn.execute(
                "UPDATE migration_ops SET state = 'failed', detail = ?, attempts = attempts + 1,"
                " updated_at = ? WHERE migration = 'merge_files' AND subject = ?",
                (str(e)[:200], datetime.now().isoformat(), target_pid))
            conn.commit()
    return done


def repoint_index(collection, source_pid, target_pid, target_name):
    """Point the source's chunks at the survivor. Metadata only.

    Chunk ids are opaque - note_chunk_id() builds them from the CF at upsert
    time, but nothing reads identity back out of an id. The metadata is what
    staff Q&A cites, so leaving it stale would attribute the survivor's own
    notes to a patient who no longer exists.
    """
    found = collection.get(where={"patient_id": source_pid})
    ids = found.get("ids") or []
    if not ids:
        return 0
    updated = []
    for meta in found.get("metadatas") or []:
        fresh = dict(meta)
        fresh["patient_id"] = target_pid
        fresh["patient_name"] = target_name
        updated.append(fresh)
    collection.update(ids=ids, metadatas=updated)
    return len(ids)


def timeline(conn, key, show_clinical=True):
    """One date-ordered record of everything that happened to a patient.

    The detail page already has a card per relation - visits here, appointments
    there, invoices below - and none of them answer "what happened to this
    person, in order", which is the question a clinician actually asks.

    NO TOTAL AND NO BALANCE. Invoice lines appear as what was billed on a
    visit, and nothing anywhere adds them up. `invoices` carries no payment
    status and there is no payments table, so any total would be a claim the
    data cannot support - which is the exact defect P02 closed in the patient
    chat. The ledger is P07's job.

    show_clinical follows the caller's own gate: an assistant holds read_notes
    but not read_clinical, so they get the shape of the history - a visit
    happened on this date - without the clinical text.
    """
    import patient_id as _pid

    pid = _pid.resolve(conn, key)
    if pid is None:
        return []
    events = []

    for v in conn.execute(
            "SELECT id, visit_date, procedures, clinical_notes FROM visits"
            " WHERE patient_id = ? ORDER BY visit_date IS NULL, visit_date, id", (pid,)):
        procedures = json.loads(v["procedures"]) if v["procedures"] else []
        events.append({
            "date": v["visit_date"],
            "kind": "visit",
            "title": ", ".join(procedures) if procedures else "Visit",
            "detail": (v["clinical_notes"] or "") if show_clinical else "",
            "source": "from a filed note",
        })

    for a in conn.execute(
            "SELECT starts_at, minutes, status, dentist FROM appointments"
            " WHERE patient_id = ? ORDER BY starts_at", (pid,)):
        # a request carries a date and a period, never a time - so the time
        # part of starts_at is meaningless on it and must not be rendered
        requested = a["status"] == "requested"
        events.append({
            "date": a["starts_at"][:10],
            "kind": "appointment",
            "title": {"booked": "Appointment", "requested": "Requested an appointment",
                      "cancelled": "Appointment cancelled",
                      "declined": "Request declined"}.get(a["status"], a["status"]),
            "detail": "" if requested else
                      f"{a['starts_at'][11:16]} with {a['dentist']} ({a['minutes']} min)",
            "source": "scheduling",
        })

    for inv in conn.execute(
            "SELECT i.amount, i.description, v.visit_date FROM invoices i"
            " JOIN visits v ON v.id = i.visit_id"
            " WHERE i.patient_id = ? ORDER BY v.visit_date, i.line_index", (pid,)):
        events.append({
            "date": inv["visit_date"],
            "kind": "billed",
            "title": f"Billed {inv['amount']:.2f} EUR",
            "detail": inv["description"] or "",
            # said on every single line, not once at the bottom, because the
            # line is what gets read aloud to a patient over the phone
            "source": "billed, not collected - payments are not recorded here",
        })

    for mrg in conn.execute(
            "SELECT source_cf, merged_at, merged_by, source_row FROM patient_merges"
            " WHERE target_patient_id = ? ORDER BY merged_at", (pid,)):
        was = json.loads(mrg["source_row"])
        events.append({
            "date": mrg["merged_at"][:10],
            "kind": "merge",
            "title": f"Absorbed the record of {was.get('patient_name') or mrg['source_cf']}",
            "detail": f"{mrg['source_cf']}, merged by {mrg['merged_by']}",
            # a record that absorbed another says so on its own history. a
            # merge that is only visible in an audit log is a merge nobody
            # reading the record will ever know happened.
            "source": "record merge",
        })

    # undated rows sort last rather than crashing the sort or claiming a date
    events.sort(key=lambda e: (e["date"] is None, e["date"] or ""))
    return events


def pending_index_repoints(conn):
    """Merges whose index step never ran. The recovery hook for MERGE-2.

    A merge completes in SQLite even when Chroma is unreachable - refusing the
    whole merge because the search index is down would be worse. What must not
    happen is the skip going unrecorded, because then the chunks keep citing a
    patient who no longer exists and nothing says which merges to fix.
    """
    out = []
    for row in conn.execute(
            "SELECT source_cf, target_cf, target_patient_id, source_row, moved"
            " FROM patient_merges ORDER BY merged_at"):
        if json.loads(row["moved"]).get("index_pending"):
            out.append({"source_cf": row["source_cf"], "target_cf": row["target_cf"],
                        "target_patient_id": row["target_patient_id"],
                        # the folded patient's own surrogate, kept in the
                        # source row - it is how its chunks are found
                        "source_pid": json.loads(
                            row["source_row"] or "{}").get("patient_id")})
    return out


def repair_index(conn, collection):
    """Re-run every index repoint that was skipped. Safe to run repeatedly."""
    done = []
    for job in pending_index_repoints(conn):
        target = conn.execute(
            "SELECT patient_name FROM patients WHERE patient_id = ?",
            (job["target_patient_id"],)).fetchone()
        if target is None:
            continue    # the survivor was itself merged away; the flatten
                        # rewrote target_patient_id, so the next pass picks it up
        if not job["source_pid"]:
            # a merge recorded before the surrogate existed. its chunks cannot
            # be found by patient_id, so this is left pending and visible
            # rather than being marked repaired on no evidence.
            continue
        count = repoint_index(collection, job["source_pid"], job["target_patient_id"],
                              target["patient_name"])
        row = conn.execute("SELECT moved FROM patient_merges WHERE source_cf = ?",
                           (job["source_cf"],)).fetchone()
        history = json.loads(row["moved"])
        history["index_chunks"] = count
        history.pop("index_pending", None)
        conn.execute("UPDATE patient_merges SET moved = ? WHERE source_cf = ?",
                     (json.dumps(history), job["source_cf"]))
        done.append({**job, "chunks": count})
    conn.commit()
    return done


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
        _pidmod.seed_patient(conn, "RSPS850010150900", "Paola Rossi", None)
        _pidmod.seed_patient(conn, "RSSP850010150900", "paola rossi", "555 0000")
        _pidmod.seed_patient(conn, "BNCG800010150100", "Giulia Bianchi", None)
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
        # order must not matter, or the same pair comes back reversed. the
        # pair is keyed on the SURROGATES now, so a dismissal survives either
        # codice fiscale being corrected later.
        _a = _pidmod.resolve(conn, "RSPS850010150900")
        _b = _pidmod.resolve(conn, "RSSP850010150900")
        assert dismissed_pairs(conn) == {tuple(sorted((_a, _b)))}, \
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

        # --- the merge (plan 2) -------------------------------------------
        #
        # a separate database. the checks above leave a dismissal and three
        # patients behind, and every assertion here is about exact counts.
        m = storage.init_db(str(Path(tmp) / "merge.sqlite"))
        KEEP, GONE, OTHER = "AAAA000000000001", "AAAA000000000002", "BBBB000000000003"

        pids = {}

        def seed(cf, name):
            pids[cf] = _pidmod.seed_patient(m, cf, name)
            return pids[cf]

        def relations(cf):
            # BY THE SURROGATE CAPTURED AT SEED TIME, not by resolving the
            # codice fiscale: after a merge the folded CF resolves to the
            # SURVIVOR, so resolving here would count the survivor's rows and
            # check 14 would pass while nothing had moved.
            pid = pids.get(cf) or _pidmod.resolve(m, cf)
            return {t: m.execute(
                f"SELECT COUNT(*) c FROM {t} WHERE patient_id = ?", (pid,)
            ).fetchone()["c"] for t in MERGE_RELATIONS}

        def give(cf, n=1, hour=9):
            for i in range(n):
                m.execute(
                    "INSERT INTO visits (patient_id, visit_date, procedures,"
                    " clinical_notes, next_appointment, source_path)"
                    " VALUES (?, '2026-01-01', '[]', 'note', NULL, ?)",
                    (_pidmod.resolve(m, cf), f"sorted/{cf}/notes/n{i}.json"))
                # each fixture patient gets its OWN hour, passed in rather
                # than derived: P05 added a unique index over live rows and it
                # caught this immediately - two patients were being booked into
                # the identical slot, the exact thing the index exists to stop.
                # hash(cf) would also work until PYTHONHASHSEED changed it.
                m.execute(
                    "INSERT INTO appointments (patient_id, dentist, starts_at, minutes,"
                    " status, created_at, updated_at)"
                    " VALUES (?, 'dr rossi', ?, 30, 'booked', '2026-01-01', '2026-01-01')",
                    (_pidmod.resolve(m, cf), f"2026-0{i + 1}-05T{hour + i:02d}:00:00"))

        for _cf, _name in ((KEEP, "Paola Rossi"), (GONE, "paola rossi"),
                           (OTHER, "Giulia Bianchi")):
            seed(_cf, _name)
        give(KEEP, 1, hour=9)
        give(GONE, 2, hour=11)
        give(OTHER, 1, hour=15)
        m.commit()
        keep_before = relations(KEEP)
        gone_before = relations(GONE)
        other_before = relations(OTHER)

        # 11. THE WITHHOLD. a merge is the most destructive operation in the
        # product, and reception must not hold it. asserted on the data, not
        # only the return value.
        ok11, msg11 = merge(m, GONE, KEEP, "areception", "assistant")
        assert not ok11 and "not permitted" in msg11, "11: an assistant must not merge"
        assert relations(GONE) == gone_before, "11: a refused merge moves nothing"
        assert m.execute("SELECT COUNT(*) c FROM patients").fetchone()["c"] == 3, \
            "11: and deletes nobody"
        denied11 = m.execute(
            "SELECT * FROM audit_log WHERE action = 'merge_patient' AND allowed = 0").fetchall()
        assert denied11 and denied11[0]["username"] == "areception", \
            "11: a refused merge is audited, naming who was refused"

        # 12. the refusals that stop a nonsense merge before it starts
        for a, b, why in ((KEEP, KEEP, "into itself"),
                          ("NOSUCHPATIENT", KEEP, "from a record that does not exist"),
                          (KEEP, "NOSUCHPATIENT", "into a record that does not exist"),
                          ("", KEEP, "with no source")):
            ok12, _ = merge(m, a, b, "anadmin", "admin")
            assert not ok12, f"12: a merge {why} must be refused"
        assert m.execute("SELECT COUNT(*) c FROM patients").fetchone()["c"] == 3, \
            "12: and none of them wrote anything"

        # 13. ATOMICITY. with the appointments repoint made to raise, NOTHING
        # may have moved - not the visits that were repointed before it, not
        # the credential, not the patients row. this is the check that makes
        # the single transaction real rather than decorative.
        class FailsOnAppointments:
            # sqlite3.Connection.execute is read-only, so the failure is
            # injected through a proxy rather than by monkeypatching. the
            # rollback still lands on the real connection underneath.
            def __init__(self, conn):
                self._conn = conn

            def __getattr__(self, name):
                return getattr(self._conn, name)

            def execute(self, sql, *args):
                if sql.startswith("UPDATE appointments"):
                    raise RuntimeError("appointments repoint failed")
                return self._conn.execute(sql, *args)

        try:
            merge(FailsOnAppointments(m), GONE, KEEP, "anadmin", "admin")
            raise AssertionError("13: the failure must propagate, not be swallowed")
        except RuntimeError:
            pass
        assert relations(GONE) == gone_before, "13: a failed merge rolls the visits back too"
        assert relations(KEEP) == keep_before, "13: and the survivor gains nothing"
        assert m.execute("SELECT COUNT(*) c FROM patients WHERE codice_fiscale = ?",
                         (GONE,)).fetchone()["c"] == 1, "13: the source row survives a failure"
        assert m.execute("SELECT COUNT(*) c FROM patient_merges").fetchone()["c"] == 0, \
            "13: and no mapping row is left behind"

        # 14. the clean merge. every relation moves, and the counts are kept so
        # a human can see exactly what happened.
        ok14, msg14 = merge(m, GONE, KEEP, "anadmin", "admin")
        assert ok14, f"14: an admin may merge - {msg14}"
        after = relations(KEEP)
        for table in MERGE_RELATIONS:
            assert after[table] == keep_before[table] + gone_before[table], \
                f"14: {table} should have moved to the survivor"
        assert relations(GONE) == {t: 0 for t in MERGE_RELATIONS}, \
            "14: nothing is left pointing at the merged record"
        assert relations(OTHER) == other_before, \
            "14: AN UNRELATED PATIENT MUST NOT BE TOUCHED"
        row14 = m.execute("SELECT * FROM patient_merges WHERE source_cf = ?", (GONE,)).fetchone()
        assert row14["target_cf"] == KEEP and row14["merged_by"] == "anadmin"
        assert json.loads(row14["moved"])["visits"] == gone_before["visits"], \
            "14: the counts moved are recorded"
        assert json.loads(row14["source_row"])["patient_name"] == "paola rossi", \
            "14: and the whole source row is kept - this is what makes it not a trace-free delete"
        assert m.execute("SELECT * FROM audit_log WHERE action = 'merge_patient'"
                         " AND allowed = 1").fetchall(), "14: a merge is audited"

        # 15. THE OLD CODICE FISCALE STILL RESOLVES. an old link, an old audit
        # row and an old file path all name it, and 404 would make the merge a
        # trace-free delete in every surface that matters.
        assert merge_target(m, GONE) == KEEP, "15: the merged CF resolves to the survivor"
        assert merge_target(m, KEEP) is None, "15: a live record resolves to nothing"
        assert merge_target(m, OTHER) is None, "15: and so does an unrelated one"
        assert merged_sources_of(m, KEEP) == [GONE], "15: the survivor knows what it absorbed"

        # 16. a second merge of the same source is refused, and so is merging
        # away a record that others were merged INTO - that would strand their
        # mapping at a CF with no row.
        ok16, msg16 = merge(m, GONE, OTHER, "anadmin", "admin")
        assert not ok16 and "already been merged" in msg16, "16: no double merge"

        # 17. MERGING A SURVIVOR ONWARD FLATTENS THE CHAIN. staff merge A into
        # B, then find B is also a duplicate of C. A must not be stranded
        # pointing at a record that no longer exists - and it cannot be, since
        # patient_merges.target_cf is a foreign key onto patients and the
        # delete would fail outright.
        ok17, msg17 = merge(m, KEEP, OTHER, "anadmin", "admin")
        assert ok17, f"17: a survivor may itself be merged onward - {msg17}"
        assert merge_target(m, KEEP) == OTHER, "17: the survivor now resolves onward"
        assert merge_target(m, GONE) == OTHER, \
            "17: AND SO DOES WHAT IT HAD ABSORBED - one hop, not a dangling pointer"
        assert set(merged_sources_of(m, OTHER)) == {KEEP, GONE}, \
            "17: the final survivor knows everything it holds"
        assert m.execute("SELECT COUNT(*) c FROM patients WHERE codice_fiscale = ?",
                         (KEEP,)).fetchone()["c"] == 0, "17: and the middle record is gone"
        # the rewrite kept the history rather than erasing it
        hist = json.loads(m.execute(
            "SELECT moved FROM patient_merges WHERE source_cf = ?", (GONE,)).fetchone()["moved"])
        assert hist["original_target"] == KEEP, \
            "17: the original target is still recorded on the rewritten row"

        # 17b. THE INDEX MOVES WITH THE ROWS. staff Q&A queries Chroma with no
        # where-clause and attributes each answer from the chunk metadata, so
        # a merge that repoints SQLite and leaves the metadata alone makes the
        # survivor's own notes get cited under a patient who no longer exists.
        # this runs against a real collection: a stubbed one would pass with
        # the repoint deleted, which is exactly the mutation it has to catch.
        import chromadb
        from chromadb.config import Settings as _Settings

        client = chromadb.PersistentClient(
            path=str(Path(tmp) / "chroma"), settings=_Settings(anonymized_telemetry=False))
        coll = client.get_or_create_collection(name="patient_notes")
        seed("EEEE000000000006", "Survivor Rossi")
        seed("FFFF000000000007", "survivor rossi")
        m.commit()
        # chunks are keyed on the SURROGATE since Phase 51
        pid_keep17, pid_gone17 = pids["EEEE000000000006"], pids["FFFF000000000007"]
        coll.upsert(ids=["EEEE:n0"], documents=["kept note"],
                    metadatas=[{"patient_id": pid_keep17,
                                "patient_name": "Survivor Rossi"}])
        coll.upsert(ids=["FFFF:n0", "FFFF:n1"], documents=["moved note", "another"],
                    metadatas=[{"patient_id": pid_gone17,
                                "patient_name": "survivor rossi"}] * 2)

        ok17b, _ = merge(m, "FFFF000000000007", "EEEE000000000006",
                         "anadmin", "admin", collection=coll)
        assert ok17b, "17b: the merge itself should succeed"
        left = coll.get(where={"patient_id": pid_gone17})
        assert left["ids"] == [], \
            "17b: NO chunk may still carry the merged-away identity"
        kept = coll.get(where={"patient_id": pid_keep17})
        assert len(kept["ids"]) == 3, \
            f"17b: the survivor should hold all 3 chunks, got {len(kept['ids'])}"
        assert all(md["patient_name"] == "Survivor Rossi" for md in kept["metadatas"]), \
            "17b: and every chunk must be attributed to the surviving patient"
        recorded = json.loads(m.execute(
            "SELECT moved FROM patient_merges WHERE source_cf = ?",
            ("FFFF000000000007",)).fetchone()["moved"])
        assert recorded["index_chunks"] == 2, \
            "17b: the number of chunks repointed is recorded with the merge"

        # 17c. MERGE-2. an unreachable index must not fail the merge - refusing
        # because search is down would be worse - but the skip has to leave a
        # record, or the chunks keep citing a folded patient and nothing says
        # which merges need fixing.
        seed("GGGG000000000008", "Pending Rossi")
        seed("HHHH000000000009", "pending rossi")
        m.commit()
        ok17c, _ = merge(m, "HHHH000000000009", "GGGG000000000008",
                         "anadmin", "admin", collection=None)
        assert ok17c, "17c: an unreachable index must not fail the merge"
        skipped = json.loads(m.execute(
            "SELECT moved FROM patient_merges WHERE source_cf = ?",
            ("HHHH000000000009",)).fetchone()["moved"])
        assert skipped["index_pending"] is True, \
            "17c: a skipped index repoint MUST be recorded, or it cannot be found again"
        # every merge above also ran without a collection, so this one is not
        # alone in the list - what matters is that it IS in it
        assert "HHHH000000000009" in [j["source_cf"] for j in pending_index_repoints(m)], \
            "17c: and it must be listed as pending"

        coll2 = client.get_or_create_collection(name="pending_notes")
        pid_gone17c = pids["HHHH000000000009"]
        coll2.upsert(ids=["HHHH:n0"], documents=["stranded note"],
                     metadatas=[{"patient_id": pid_gone17c,
                                 "patient_name": "pending rossi"}])
        fixed = repair_index(m, coll2)
        mine = [f for f in fixed if f["source_cf"] == "HHHH000000000009"]
        assert len(mine) == 1 and mine[0]["chunks"] == 1, \
            f"17c: the repair must repoint this merge's chunk, got {fixed}"
        assert coll2.get(where={"patient_id": pid_gone17c})["ids"] == [], \
            "17c: after the repair no chunk carries the folded identity"
        assert pending_index_repoints(m) == [], "17c: and nothing is left pending"
        assert repair_index(m, coll2) == [], "17c: re-running the repair is a no-op"

        # 18. a merged pair leaves the review screen - the question has been
        # answered and must not be asked again
        assert all(GONE not in (c["a"]["codice_fiscale"], c["b"]["codice_fiscale"])
                   for c in candidates(m)), "18: a merged record is not a duplicate candidate"

        # --- the record timeline (plan 3) ---------------------------------
        t = storage.init_db(str(Path(tmp) / "timeline.sqlite"))
        TCF = "TTTT000000000001"
        TPID = _pidmod.seed_patient(t, TCF, "Timeline Rossi")
        t.execute("INSERT INTO visits (patient_id, visit_date, procedures,"
                  " clinical_notes, next_appointment, source_path)"
                  " VALUES (?, '2026-03-02', '[\"comp 20\"]', 'filled the tooth', NULL, 't1.json')",
                  (TPID,))
        vid = t.execute("SELECT id FROM visits WHERE patient_id = ?", (TPID,)).fetchone()["id"]
        t.execute("INSERT INTO invoices (patient_id, visit_id, line_index, amount, description)"
                  " VALUES (?, ?, 0, 80.0, 'composite filling')", (TPID, vid))
        t.execute("INSERT INTO appointments (patient_id, dentist, starts_at, minutes,"
                  " status, created_at, updated_at)"
                  " VALUES (?, 'dr rossi', '2026-05-10T14:30:00', 30, 'booked', '', '')", (TPID,))
        t.execute("INSERT INTO appointments (patient_id, dentist, starts_at, minutes,"
                  " status, period, created_at, updated_at)"
                  " VALUES (?, '', '2026-01-04T00:00:00', 0, 'requested', 'morning', '', '')", (TPID,))
        t.commit()

        # 19. one list, in date order, across every relation
        tl = timeline(t, TPID)
        assert [e["date"] for e in tl] == sorted(e["date"] for e in tl), \
            "19: the timeline must be in date order"
        kinds = [e["kind"] for e in tl]
        assert kinds.count("visit") == 1 and kinds.count("appointment") == 2 \
            and kinds.count("billed") == 1, f"19: every relation should appear, got {kinds}"
        assert tl[0]["kind"] == "appointment" and tl[0]["date"] == "2026-01-04", \
            "19: the january request comes first"

        # 20. A REQUEST IS NEVER GIVEN A TIME. it carries a date and a period,
        # so the time half of starts_at is meaningless - rendering 00:00 would
        # be inventing an appointment the clinic never offered.
        req = [e for e in tl if e["title"] == "Requested an appointment"][0]
        assert "00:00" not in req["detail"] and req["detail"] == "", \
            f"20: a request must not be shown with a time, got {req['detail']!r}"
        booked = [e for e in tl if e["title"] == "Appointment"][0]
        assert "14:30" in booked["detail"], "20: a real booking does show its time"

        # 21. NO TOTAL. invoices carry no payment status and there is no
        # payments table, so any sum would be a claim the data cannot support -
        # the same defect P02 closed in the patient chat, in a new surface.
        billed = [e for e in tl if e["kind"] == "billed"][0]
        assert "80.00" in billed["title"], "21: the billed line shows what was billed"
        assert "not collected" in billed["source"], \
            "21: and says so on the line, not once at the bottom of the page"
        joined = " ".join(e["title"] + e["detail"] + e["source"] for e in tl).lower()
        for forbidden in ("total", "balance", "owes", "outstanding", "due"):
            assert forbidden not in joined, \
                f"21: the timeline must never present a {forbidden!r}"

        # 22. an assistant holds read_notes but not read_clinical, so they see
        # THAT a visit happened without reading what it said
        plain = timeline(t, TPID, show_clinical=False)
        assert all("filled the tooth" not in e["detail"] for e in plain), \
            "22: clinical text must follow the caller's own gate"
        assert [e["kind"] for e in plain] == kinds, \
            "22: but the shape of the history is still there"

        # 23. a merge appears on the survivor's own history. one that is only
        # in an audit log is one nobody reading the record will ever know about.
        _pidmod.seed_patient(t, "TTTT000000000002", "timeline rossi")
        t.commit()
        merge(t, "TTTT000000000002", TCF, "anadmin", "admin")
        merged_tl = timeline(t, TPID)
        note = [e for e in merged_tl if e["kind"] == "merge"]
        assert len(note) == 1, "23: the merge must show on the surviving record"
        assert "timeline rossi" in note[0]["title"], "23: naming what it absorbed"
        assert "anadmin" in note[0]["detail"], "23: and who did it"

        # 24. an undated visit sorts last instead of crashing or claiming a date
        t.execute("INSERT INTO visits (patient_id, visit_date, procedures,"
                  " clinical_notes, next_appointment, source_path)"
                  " VALUES (?, NULL, '[]', 'no date on this one', NULL, 't2.json')", (TPID,))
        t.commit()
        undated = timeline(t, TPID)
        assert undated[-1]["date"] is None, "24: an undated row sorts last"
        assert len(undated) == len(merged_tl) + 1, "24: and is not dropped"

    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    print("usage: python patient_identity.py --selftest")


if __name__ == "__main__":
    main()
