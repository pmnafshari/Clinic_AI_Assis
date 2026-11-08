"""P29 NEW-07 / E-008: the dental chart. FDI (ISO 3950) numbering, as dental software in Europe uses it: two digits,
the quadrant then the tooth from the midline - 11-18 upper right, 21-28 upper left, 31-38 lower left, 41-48 lower
right; baby teeth 51-55, 61-65, 71-75, 81-85. Five surfaces per tooth: mesial, occlusal (incisal on front teeth),
distal, buccal (vestibular), lingual (palatal on upper teeth), stored as M O D B L.

Every mark is a kind of work on one tooth, done or planned. The notes propose marks - a procedure line that names a
treatment and a tooth ("comp 36 MOD", "ext 47 48", "otturazione 14") - but a proposal is never drawn until the
dentist approves it; a refused one is not proposed again. Only the dentist reads or changes the chart.
"""
import json
import re
import sys

import clinic_time

SCHEMA = """
    CREATE TABLE IF NOT EXISTS tooth_marks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_id TEXT NOT NULL,
        tooth INTEGER NOT NULL,
        surfaces TEXT NOT NULL DEFAULT '',
        kind TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'done',
        status TEXT NOT NULL,
        source TEXT NOT NULL,
        visit_id INTEGER,
        said TEXT,
        note TEXT,
        marked_on TEXT,
        created_by TEXT,
        created_at TEXT NOT NULL,
        decided_by TEXT,
        decided_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_tooth_marks_patient ON tooth_marks (patient_id);
    CREATE UNIQUE INDEX IF NOT EXISTS idx_tooth_marks_from_note
        ON tooth_marks (patient_id, visit_id, tooth, kind) WHERE source = 'note';
"""

UPPER = [18, 17, 16, 15, 14, 13, 12, 11, 21, 22, 23, 24, 25, 26, 27, 28]
LOWER = [48, 47, 46, 45, 44, 43, 42, 41, 31, 32, 33, 34, 35, 36, 37, 38]
BABY_UPPER = [55, 54, 53, 52, 51, 61, 62, 63, 64, 65]
BABY_LOWER = [85, 84, 83, 82, 81, 71, 72, 73, 74, 75]
TEETH = UPPER + LOWER + BABY_UPPER + BABY_LOWER

SURFACES = "MODBL"
STATES = ("done", "planned")

# kind -> its short code on the chart and how it is drawn: "fill" colours the surfaces it names, "ring" circles the
# crown, "root" fills the root, "cross" strikes the tooth out, and so on. the colours are in app.css (tc-k-<kind>)
KINDS = {
    "caries": ("C", "fill"),
    "filling": ("F", "fill"),
    "temporary": ("T", "fill"),
    "sealant": ("S", "fill"),
    "inlay": ("In", "fill"),
    "veneer": ("V", "buccal"),
    "crown": ("Cr", "ring"),
    "bridge": ("Br", "ring"),
    "denture": ("De", "ring"),
    "rct": ("RC", "root"),
    "post": ("P", "root"),
    "implant": ("Im", "implant"),
    "extraction": ("X", "cross"),
    "missing": ("Mi", "cross"),
    "fracture": ("Fr", "crack"),
    "perio": ("Pe", "gum"),
    "impacted": ("Ip", "dashed"),
    "other": ("?", "badge"),
}

# what a procedure line may call each kind, in English and Italian, with the shorthand the notes model writes. a
# longer phrase beats a shorter one inside it ("otturazione provvisoria" is temporary, not a filling)
WORDS = {
    "rct": ("rct", "root canal", "endo", "endodontic", "devitalizzazione", "devitalizzato", "devitalizzata",
            "cura canalare", "canalare"),
    "extraction": ("ext", "extraction", "extracted", "extract", "estrazione", "estrarre", "estratto", "estratta", "avulsione"),
    "filling": ("filling", "comp", "composite", "restoration", "otturazione", "otturato", "otturata", "composito",
                "ricostruzione", "restauro"),
    "temporary": ("temporary filling", "otturazione provvisoria", "provvisoria", "provvisorio"),
    "caries": ("caries", "carie", "cavity", "decay", "tooth decay"),
    "crown": ("crown", "corona", "capsula"),
    "sealant": ("seal", "sealant", "fissure sealant", "sigillatura", "sigillato"),
    "implant": ("implant", "impianto"),
    "bridge": ("bridge", "ponte"),
    "veneer": ("veneer", "faccetta"),
    "inlay": ("inlay", "onlay", "intarsio"),
    "post": ("post", "perno", "perno moncone"),
    "fracture": ("fracture", "fractured", "frattura", "fratturato", "fratturata"),
    "perio": ("perio", "periodontal", "levigatura", "parodontale", "scaling and root planing"),
    "impacted": ("impacted", "incluso", "inclusa"),
    "denture": ("denture", "protesi"),
    "missing": ("missing", "mancante", "assente"),
}
NOT_WORDS = ("no", "non", "not", "without", "nessuna", "nessun", "senza")
PLANNED_WORDS = ("planned", "to do", "to extract", "da fare", "da estrarre", "programmato", "programmata",
                 "previsto", "prevista", "da eseguire")
TOOTH_RX = re.compile(r"(?<!\d)([1-8][1-8])(?!\d)")
SURFACE_RX = re.compile(r"(?<![A-Za-z])([MODBLVPI]{1,5})(?![A-Za-z])")


def is_tooth(n):
    return n in TEETH


def is_baby(tooth):
    return tooth // 10 >= 5


def surface_kind(kind):
    return KINDS.get(kind, ("", ""))[1] == "fill"


def canon_surfaces(letters):
    """M O D B L in that order, from what a form or a note gives - V is buccal, P lingual (palatal), I occlusal
    (incisal)"""
    given = set((letters or "").upper().replace("V", "B").replace("P", "L").replace("I", "O"))
    return "".join(s for s in SURFACES if s in given)


def surface_names(tooth, surfaces):
    """the surfaces as words, named as on that tooth: incisal on a front tooth, palatal on an upper one"""
    front = tooth % 10 <= 3
    upper = tooth // 10 in (1, 2, 5, 6)
    names = {"M": "mesial", "O": "incisal" if front else "occlusal", "D": "distal", "B": "buccal",
             "L": "palatal" if upper else "lingual"}
    return [names[s] for s in surfaces]


def layout(tooth):
    """which surface each side of the drawn crown is. the chart is the dentist's view of the patient: the patient's
    right on the left, the outer side of each arch at the chart's edge, mesial towards the midline"""
    upper = tooth // 10 in (1, 2, 5, 6)
    left_of_midline = tooth // 10 in (1, 4, 5, 8)
    return {"top": "B" if upper else "L", "bottom": "L" if upper else "B",
            "left": "D" if left_of_midline else "M", "right": "M" if left_of_midline else "D", "center": "O"}


def _words(text):
    return re.sub(r"[^\w]+", " ", (text or "").lower()).split()


def read_entry(entry):
    """[(tooth, kind, surfaces, state)] for one procedure line - nothing when it names no kind or no tooth"""
    words = _words(entry)
    best = None
    for kind, terms in WORDS.items():
        for term in terms:
            t = term.split()
            for i in range(len(words) - len(t) + 1):
                if words[i:i + len(t)] != t or (i and words[i - 1] in NOT_WORDS):
                    continue
                # the longest phrase wins, then the one written first
                if best is None or (len(t), -i) > (best[1], -best[2]):
                    best = (kind, len(t), i)
    if best is None:
        return []
    kind = best[0]
    teeth = []
    for m in TOOTH_RX.finditer(entry or ""):
        n = int(m.group(1))
        if is_tooth(n) and n not in teeth:
            teeth.append(n)
    surfaces = ""
    if surface_kind(kind):
        for m in SURFACE_RX.finditer(entry or ""):
            surfaces = canon_surfaces(surfaces + m.group(1))
    low = " ".join(words)
    state = "planned" if any(w in low for w in PLANNED_WORDS) else "done"
    return [(n, kind, surfaces, state) for n in teeth]


def propose(conn, pid):
    """read this patient's visits and keep one proposal per (visit, tooth, kind) they name. idempotent: an approved
    or refused one stays as it was decided; a proposal whose line was edited away goes"""
    derived = {}
    for v in conn.execute("SELECT id, visit_date, procedures FROM visits WHERE patient_id = ? ORDER BY id", (pid,)):
        try:
            entries = json.loads(v["procedures"] or "[]")
        except ValueError:
            entries = []
        for entry in entries:
            for tooth, kind, surfaces, state in read_entry(str(entry)):
                derived.setdefault((v["id"], tooth, kind), (surfaces, state, str(entry)[:200], v["visit_date"]))
    known = {(r["visit_id"], r["tooth"], r["kind"]): r for r in conn.execute(
        "SELECT id, visit_id, tooth, kind, status FROM tooth_marks WHERE patient_id = ? AND source = 'note'", (pid,))}
    now = clinic_time.stamp()
    for key, (surfaces, state, said, day) in derived.items():
        if key not in known:
            conn.execute("INSERT INTO tooth_marks (patient_id, tooth, surfaces, kind, state, status, source, visit_id,"
                         " said, marked_on, created_at) VALUES (?, ?, ?, ?, ?, 'proposed', 'note', ?, ?, ?, ?)",
                         (pid, key[1], surfaces, key[2], state, key[0], said, day, now))
    for key, row in known.items():
        if key not in derived and row["status"] == "proposed":
            conn.execute("DELETE FROM tooth_marks WHERE id = ?", (row["id"],))
    conn.commit()


def _mark(row):
    m = dict(row)
    m["code"], m["draw"] = KINDS.get(m["kind"], ("?", "badge"))
    m["surface_names"] = surface_names(m["tooth"], m["surfaces"])
    return m


def pending(conn, pid):
    rows = conn.execute("SELECT t.*, v.visit_date FROM tooth_marks t LEFT JOIN visits v ON v.id = t.visit_id"
                        " WHERE t.patient_id = ? AND t.status = 'proposed' ORDER BY v.visit_date, t.tooth, t.id",
                        (pid,)).fetchall()
    return [_mark(r) for r in rows]


def marks(conn, pid):
    """the chart's marks - approved or added by the dentist, never a proposal - newest first"""
    rows = conn.execute("SELECT * FROM tooth_marks WHERE patient_id = ? AND status = 'confirmed'"
                        " ORDER BY COALESCE(marked_on, '') DESC, id DESC", (pid,)).fetchall()
    return [_mark(r) for r in rows]


def chart(conn, pid):
    """tooth -> what to draw: each surface's kind and state (the newest mark on it), the whole-tooth marks, and
    every mark for the tooth's own list"""
    teeth = {n: {"tooth": n, "fills": {}, "whole": [], "marks": [], "layout": layout(n)} for n in TEETH}
    for m in reversed(marks(conn, pid)):  # oldest first, so a newer mark draws over an older one
        t = teeth.get(m["tooth"])
        if t is None:
            continue
        t["marks"].insert(0, m)
        if m["draw"] == "fill" and m["surfaces"]:
            for s in m["surfaces"]:
                t["fills"][s] = (m["kind"], m["state"])
        elif m["draw"] == "buccal":
            t["fills"]["B"] = (m["kind"], m["state"])
        else:
            t["whole"].append((m["kind"], m["state"], m["draw"]))
    return teeth


def decide(conn, pid, mark_id, approve, who):
    """approve or refuse one proposal of this patient's. True when it was still waiting"""
    cur = conn.execute("UPDATE tooth_marks SET status = ?, decided_by = ?, decided_at = ? WHERE id = ?"
                       " AND patient_id = ? AND status = 'proposed'",
                       ("confirmed" if approve else "rejected", who, clinic_time.stamp(), mark_id, pid))
    conn.commit()
    return cur.rowcount == 1


def add(conn, pid, tooth, kind, surfaces, state, who, marked_on=None, note=None):
    """the dentist's own mark, on the chart at once. (mark id, None) or (None, why not)"""
    if not is_tooth(tooth):
        return None, "that is not a tooth number"
    if kind not in KINDS:
        return None, "choose the work"
    if state not in STATES:
        return None, "choose done or planned"
    surfaces = canon_surfaces(surfaces) if surface_kind(kind) else ""
    marked_on = (marked_on or "").strip()[:10] or clinic_time.now().date().isoformat()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", marked_on):
        return None, "the date is not a date"
    note = (note or "").strip()[:200] or None
    if kind == "other" and not note:
        return None, "say what the other work is"
    now = clinic_time.stamp()
    cur = conn.execute("INSERT INTO tooth_marks (patient_id, tooth, surfaces, kind, state, status, source, note,"
                       " marked_on, created_by, created_at, decided_by, decided_at)"
                       " VALUES (?, ?, ?, ?, ?, 'confirmed', 'dentist', ?, ?, ?, ?, ?, ?)",
                       (pid, tooth, surfaces, kind, state, note, marked_on, who, now, who, now))
    conn.commit()
    return cur.lastrowid, None


def remove(conn, pid, mark_id, who):
    """take a mark off the chart. it is kept as removed, with who and when"""
    cur = conn.execute("UPDATE tooth_marks SET status = 'removed', decided_by = ?, decided_at = ? WHERE id = ?"
                       " AND patient_id = ? AND status = 'confirmed'", (who, clinic_time.stamp(), mark_id, pid))
    conn.commit()
    return cur.rowcount == 1


def exportable(conn, pid):
    """the chart as the patient's copy holds it: what is on it, never a proposal"""
    return [{"tooth": m["tooth"], "work": m["kind"], "surfaces": m["surfaces"], "state": m["state"],
             "date": m["marked_on"], "note": m["note"]} for m in marks(conn, pid)]


def on_erase(conn, pid):
    """inside the erasure transaction"""
    conn.execute("DELETE FROM tooth_marks WHERE patient_id = ?", (pid,))


def selftest():
    import sqlite3
    # 1. FDI: 32 adult and 20 baby teeth, nothing else
    assert len(set(UPPER + LOWER)) == 32 and len(set(BABY_UPPER + BABY_LOWER)) == 20, "1: tooth count"
    for n in (10, 19, 20, 29, 39, 49, 50, 56, 66, 76, 86, 90, 9, 0):
        assert not is_tooth(n), f"1: {n} is not an FDI tooth"
    assert is_tooth(46) and is_tooth(11) and is_tooth(85) and is_baby(85) and not is_baby(48), "1: real teeth"

    # 2. a procedure line becomes (tooth, kind, surfaces, state)
    cases = {
        "comp 36 MOD": [(36, "filling", "MOD", "done")],
        "ext 47 48": [(47, "extraction", "", "done"), (48, "extraction", "", "done")],
        "extraction of tooth 38": [(38, "extraction", "", "done")],
        "rct 46": [(46, "rct", "", "done")],
        "otturazione 14 OD": [(14, "filling", "OD", "done")],
        "otturazione provvisoria 25": [(25, "temporary", "", "done")],
        "caries 21 V": [(21, "caries", "B", "done")],
        "corona 11": [(11, "crown", "", "done")],
        "da estrarre 18": [(18, "extraction", "", "planned")],
        "impianto 36": [(36, "implant", "", "done")],
        "sigillatura 46 O": [(46, "sealant", "O", "done")],
        "prophy": [],                       # whole mouth, no tooth
        "pulizia melone6": [],              # no kind it knows
        "filling 19": [],                   # 19 is not a tooth
        "nessuna carie 36": [],             # said not to be there
        "lychee 3": [],
    }
    for entry, want in cases.items():
        got = read_entry(entry)
        assert got == want, f"2: {entry!r} -> {got}, want {want}"
    assert canon_surfaces("dvmP") == "MDBL" and canon_surfaces("I") == "O", "2: surface letters"

    # 3. the drawn crown: dentist's view, mesial towards the midline, outer side at the chart's edge
    assert layout(16) == {"top": "B", "bottom": "L", "left": "D", "right": "M", "center": "O"}, "3: 16"
    assert layout(26)["left"] == "M" and layout(36)["top"] == "L" and layout(46)["right"] == "M", "3: sides"
    assert surface_names(11, "OL") == ["incisal", "palatal"] and surface_names(46, "OL") == ["occlusal", "lingual"]

    # 4. proposals: one per visit, tooth and kind; never drawn until approved; a refusal is not asked again
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE visits (id INTEGER PRIMARY KEY, patient_id TEXT, visit_date TEXT, procedures TEXT)")
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO visits VALUES (1, 'P1', '2026-03-02', ?)", (json.dumps(["comp 36 MO", "prophy"]),))
    conn.execute("INSERT INTO visits VALUES (2, 'P1', '2026-04-10', ?)", (json.dumps(["ext 48", "rct 46"]),))
    conn.execute("INSERT INTO visits VALUES (3, 'P2', '2026-04-10', ?)", (json.dumps(["crown 11"]),))
    propose(conn, "P1")
    propose(conn, "P1")
    waiting = pending(conn, "P1")
    assert [(m["tooth"], m["kind"]) for m in waiting] == [(36, "filling"), (46, "rct"), (48, "extraction")], \
        f"4: {[(m['tooth'], m['kind']) for m in waiting]}"
    assert marks(conn, "P1") == [] and chart(conn, "P1")[36]["fills"] == {}, "4: HARD FAIL - a proposal drawn"
    assert pending(conn, "P2") == [], "4: another patient's notes proposed here"

    ids = {m["kind"]: m["id"] for m in waiting}
    assert decide(conn, "P1", ids["filling"], True, "dr_a") and decide(conn, "P1", ids["extraction"], False, "dr_a")
    assert not decide(conn, "P1", ids["filling"], False, "dr_a"), "4: a decided proposal decided again"
    assert not decide(conn, "P2", ids["rct"], True, "dr_a"), "4: HARD FAIL - decided through another patient"
    propose(conn, "P1")
    assert [m["kind"] for m in pending(conn, "P1")] == ["rct"], "4: a refused proposal came back"
    drawn = chart(conn, "P1")
    assert drawn[36]["fills"] == {"M": ("filling", "done"), "O": ("filling", "done")}, f"4: {drawn[36]['fills']}"
    assert drawn[36]["marks"][0]["decided_by"] == "dr_a", "4: who approved it is kept"
    assert drawn[48]["whole"] == [], "4: a refused extraction is drawn"

    # 5. a line edited away takes its waiting proposal with it; an approved mark stays
    conn.execute("UPDATE visits SET procedures = ? WHERE id = 2", (json.dumps(["ext 48"]),))
    conn.execute("UPDATE visits SET procedures = '[]' WHERE id = 1")
    propose(conn, "P1")
    assert pending(conn, "P1") == [] and len(marks(conn, "P1")) == 1, "5: edits to the notes"

    # 6. the dentist's own marks: on the chart at once, checked, removable
    mid, why = add(conn, "P1", 11, "crown", "MO", "planned", "dr_a", "2026-05-01")
    assert mid and why is None, f"6: {why}"
    t11 = chart(conn, "P1")[11]
    assert t11["whole"] == [("crown", "planned", "ring")] and t11["fills"] == {}, "6: a crown takes no surfaces"
    mid2, _ = add(conn, "P1", 21, "veneer", "", "done", "dr_a")
    assert chart(conn, "P1")[21]["fills"] == {"B": ("veneer", "done")}, "6: a veneer covers the buccal side"
    for bad in ((19, "crown", "", "done", None), (11, "laser", "", "done", None), (11, "crown", "", "soon", None),
                (11, "other", "", "done", None)):
        assert add(conn, "P1", *bad[:4], "dr_a", note=bad[4])[0] is None, f"6: {bad} accepted"
    assert add(conn, "P1", 11, "crown", "", "done", "dr_a", "1 May")[0] is None, "6: a date that is not one"
    assert add(conn, "P1", 11, "other", "", "done", "dr_a", note="splint")[0], "6: other with what it is"
    assert not remove(conn, "P2", mid, "dr_a"), "6: HARD FAIL - removed through another patient"
    assert remove(conn, "P1", mid, "dr_a") and chart(conn, "P1")[11]["whole"] == [("other", "done", "badge")], \
        "6: remove"
    assert conn.execute("SELECT status FROM tooth_marks WHERE id = ?", (mid,)).fetchone()[0] == "removed", \
        "6: a removed mark is kept as removed"
    assert {m["work"] for m in exportable(conn, "P1")} == {"filling", "veneer", "other"}, "6: the copy"
    on_erase(conn, "P1")
    assert conn.execute("SELECT COUNT(*) FROM tooth_marks WHERE patient_id = 'P1'").fetchone()[0] == 0, "6: erase"
    assert mid2
    conn.close()
    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        print("usage: python dental_chart.py --selftest")
