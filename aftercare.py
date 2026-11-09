"""P29 R-020 / NEW-05: aftercare for each treatment, in Italian and English, written or approved by a dentist.

A text is a version of one topic (extraction, filling, root canal, crown, implant, cleaning, gum treatment) in one
language. It starts as a draft; a dentist approves it, and only then does it reach a patient - on the portal's home
page and as the assistant's answer to an aftercare question - for a treatment in the patient's own visits of the last
30 days. Approving a new version retires the one before; every version is kept. The texts that ship are DEMO
SAMPLES, marked so, and none of them is approved: a clinic's aftercare is a clinician's to write.
"""
import json
import re
import sys
from datetime import timedelta

import clinic_time

SCHEMA = """
    CREATE TABLE IF NOT EXISTS aftercare_texts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        topic TEXT NOT NULL,
        lang TEXT NOT NULL,
        version INTEGER NOT NULL,
        body TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'draft',
        demo INTEGER NOT NULL DEFAULT 0,
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        approved_by TEXT,
        approved_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_aftercare_topic ON aftercare_texts (topic, lang, status);
"""

TOPICS = ("extraction", "filling", "rct", "crown", "implant", "cleaning", "perio")
LANGS = ("it", "en")
DAYS = 30   # how long after a visit its aftercare is shown

# what a visit's procedure line calls each topic, English, Italian and the notes' shorthand
WORDS = {
    "extraction": ("ext", "extraction", "extracted", "estrazione", "estratto", "estratta", "avulsione"),
    "filling": ("filling", "comp", "composite", "restoration", "otturazione", "composito", "restauro", "ricostruzione"),
    "rct": ("rct", "root canal", "endo", "devitalizzazione", "canalare"),
    "crown": ("crown", "corona", "capsula"),
    "implant": ("implant", "impianto"),
    "cleaning": ("prophy", "cleaning", "scaling", "pulizia", "igiene", "detartrasi", "ablazione", "tartaro"),
    "perio": ("perio", "periodontal", "levigatura", "parodontale"),
}
# the assistant's aftercare questions (patient_faq) and the topics each one is about
FAQ_TOPICS = {"after_extraction": ("extraction",), "after_filling": ("filling",), "aftercare_general": TOPICS}

DEMO = {
    "extraction": {
        "it": "Tieni la garza morsa per 30 minuti. Per 24 ore non sciacquare, non fumare e non bere con la cannuccia. "
              "Mangia cibi morbidi e tiepidi dal lato opposto. Se il sanguinamento non si ferma o il dolore aumenta "
              "dopo tre giorni, chiama la clinica.",
        "en": "Keep biting on the gauze for 30 minutes. For 24 hours do not rinse, smoke or drink through a straw. Eat "
              "soft, lukewarm food on the other side. If the bleeding does not stop or the pain grows after three "
              "days, call the clinic."},
    "filling": {
        "it": "Se hai avuto l'anestesia, non mangiare finché non passa. La sensibilità al freddo per qualche giorno è "
              "normale. Se il morso ti sembra alto, chiama la clinica.",
        "en": "If you had an anaesthetic, do not eat until it wears off. Some sensitivity to cold for a few days is "
              "normal. If your bite feels high, call the clinic."},
    "rct": {
        "it": "Il dente può essere dolente per qualche giorno. Evita di masticare cibi duri da quel lato finché non è "
              "pronta la ricostruzione definitiva. Se compare gonfiore, chiama la clinica.",
        "en": "The tooth may be sore for a few days. Avoid chewing hard food on that side until the final restoration "
              "is done. If it swells, call the clinic."},
    "crown": {
        "it": "Con una corona provvisoria evita cibi appiccicosi e il filo interdentale tirato verso l'alto. Se la "
              "corona si stacca, conservala e chiama la clinica.",
        "en": "With a temporary crown avoid sticky food and pull dental floss out sideways. If the crown comes off, "
              "keep it and call the clinic."},
    "implant": {
        "it": "Per due giorni applica il freddo a intervalli e mangia cibi morbidi. Non toccare la zona con la lingua o "
              "le dita. Se il dolore o il gonfiore aumentano dopo tre giorni, chiama la clinica.",
        "en": "For two days use a cold pack on and off and eat soft food. Do not touch the area with your tongue or "
              "fingers. If the pain or swelling grows after three days, call the clinic."},
    "cleaning": {
        "it": "Le gengive possono sanguinare un poco per un giorno. Continua a lavare i denti due volte al giorno e "
              "usa il filo interdentale.",
        "en": "Your gums may bleed a little for a day. Keep brushing twice a day and use floss."},
    "perio": {
        "it": "Le gengive possono essere sensibili per qualche giorno. Spazzola con delicatezza e segui i controlli "
              "fissati.",
        "en": "Your gums may be tender for a few days. Brush gently and keep the follow-up visits."},
}


def seed_demo(conn):
    """put the demo samples in as drafts, once: a topic and language with any version already is left alone"""
    for topic, texts in DEMO.items():
        for lang, body in texts.items():
            if conn.execute("SELECT 1 FROM aftercare_texts WHERE topic = ? AND lang = ?", (topic, lang)).fetchone():
                continue
            conn.execute("INSERT INTO aftercare_texts (topic, lang, version, body, status, demo, created_by, created_at)"
                         " VALUES (?, ?, 1, ?, 'draft', 1, 'demo', ?)", (topic, lang, body, clinic_time.stamp()))
    conn.commit()


def _words(text):
    return " " + " ".join(re.sub(r"[^\w]+", " ", (text or "").lower()).split()) + " "


def topics_in(procedures):
    """the topics a visit's procedure lines name, in TOPICS order"""
    text = _words(" ".join(str(p) for p in procedures or []))
    return [t for t in TOPICS if any(f" {w} " in text for w in WORDS[t])]


def approved(conn, topic, lang):
    row = conn.execute("SELECT * FROM aftercare_texts WHERE topic = ? AND lang = ? AND status = 'approved'",
                       (topic, lang)).fetchone()
    return dict(row) if row else None


def save_draft(conn, topic, lang, body, who):
    """a new draft version; any older draft of the topic and language is retired. (id, None) or (None, why)"""
    body = (body or "").strip()[:2000]
    if topic not in TOPICS or lang not in LANGS:
        return None, "unknown topic"
    if not body:
        return None, "write the text"
    version = conn.execute("SELECT COALESCE(MAX(version), 0) + 1 FROM aftercare_texts WHERE topic = ? AND lang = ?",
                           (topic, lang)).fetchone()[0]
    conn.execute("UPDATE aftercare_texts SET status = 'retired' WHERE topic = ? AND lang = ? AND status = 'draft'",
                 (topic, lang))
    cur = conn.execute("INSERT INTO aftercare_texts (topic, lang, version, body, created_by, created_at)"
                       " VALUES (?, ?, ?, ?, ?, ?)", (topic, lang, version, body, who, clinic_time.stamp()))
    conn.commit()
    return cur.lastrowid, None


def approve(conn, text_id, who):
    """a draft becomes the text patients see; the one approved before is retired. True when it was a draft"""
    row = conn.execute("SELECT topic, lang FROM aftercare_texts WHERE id = ? AND status = 'draft'", (text_id,)).fetchone()
    if row is None:
        return False
    conn.execute("UPDATE aftercare_texts SET status = 'retired' WHERE topic = ? AND lang = ? AND status = 'approved'",
                 (row[0], row[1]))
    conn.execute("UPDATE aftercare_texts SET status = 'approved', approved_by = ?, approved_at = ? WHERE id = ?",
                 (who, clinic_time.stamp(), text_id))
    conn.commit()
    return True


def overview(conn):
    """per topic and language: the approved text and the waiting draft, for the dentist's page"""
    out = []
    for topic in TOPICS:
        row = {"topic": topic}
        for lang in LANGS:
            draft = conn.execute("SELECT * FROM aftercare_texts WHERE topic = ? AND lang = ? AND status = 'draft'"
                                 " ORDER BY version DESC LIMIT 1", (topic, lang)).fetchone()
            row[lang] = {"approved": approved(conn, topic, lang), "draft": dict(draft) if draft else None}
        out.append(row)
    return out


def for_patient(conn, pid, lang, now=None):
    """[{topic, visit_date, text (approved, or None), lang}] for the treatments in the patient's visits of the last
    DAYS days, newest visit first, one per topic. text is the approved one in the patient's language, else the
    other language's; None when no text is approved yet - then nothing is shown to the patient"""
    since = ((now or clinic_time.now()) - timedelta(days=DAYS)).date().isoformat()
    seen, out = set(), []
    for v in conn.execute("SELECT visit_date, procedures FROM visits WHERE patient_id = ? AND visit_date >= ?"
                          " ORDER BY visit_date DESC, id DESC", (pid, since)):
        try:
            procedures = json.loads(v["procedures"] or "[]")
        except ValueError:
            procedures = []
        for topic in topics_in(procedures):
            if topic in seen:
                continue
            seen.add(topic)
            text = approved(conn, topic, lang) or approved(conn, topic, "en" if lang == "it" else "it")
            out.append({"topic": topic, "visit_date": v["visit_date"], "text": text})
    return out


def answer_for(conn, pid, faq_key, lang, now=None):
    """the assistant's answer to an aftercare question: the approved texts for the treatments the question is about
    that the patient had in the last DAYS days, or None - then the question goes to a person as before"""
    topics = FAQ_TOPICS.get(faq_key)
    if not topics:
        return None
    found = [a for a in for_patient(conn, pid, lang, now) if a["topic"] in topics and a["text"]]
    return found or None


def selftest():
    import sqlite3
    from datetime import datetime
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE visits (id INTEGER PRIMARY KEY, patient_id TEXT, visit_date TEXT, procedures TEXT)")
    conn.executescript(SCHEMA)
    # 1. the demo samples go in as drafts, once, and nothing is approved
    seed_demo(conn)
    seed_demo(conn)
    assert conn.execute("SELECT COUNT(*) FROM aftercare_texts").fetchone()[0] == len(TOPICS) * 2, "1: seeded twice"
    assert conn.execute("SELECT COUNT(*) FROM aftercare_texts WHERE status != 'draft' OR demo != 1").fetchone()[0] == 0, \
        "1: HARD FAIL - a demo sample is not a draft"
    # 2. a visit's lines name their topics
    assert topics_in(["ext 48", "prophy"]) == ["extraction", "cleaning"], "2: ext and prophy"
    assert topics_in(["otturazione 36 MO", "devitalizzazione 46"]) == ["filling", "rct"], "2: Italian"
    assert topics_in(["controllo"]) == [] and topics_in(["textbook"]) == [], "2: a word inside another is not one"
    # 3. nothing reaches a patient until approved; then the approved version, newest visit first
    now = datetime(2026, 11, 9, 12, 0)
    conn.execute("INSERT INTO visits VALUES (1, 'P1', '2026-11-05', ?)", (json.dumps(["ext 48", "prophy"]),))
    conn.execute("INSERT INTO visits VALUES (2, 'P1', '2026-09-01', ?)", (json.dumps(["comp 36"]),))   # too old
    assert [(a["topic"], a["text"]) for a in for_patient(conn, "P1", "it", now)] == [("extraction", None), ("cleaning", None)]
    assert answer_for(conn, "P1", "after_extraction", "it", now) is None, "3: HARD FAIL - a draft answered a patient"
    draft = conn.execute("SELECT id FROM aftercare_texts WHERE topic = 'extraction' AND lang = 'it'").fetchone()[0]
    assert approve(conn, draft, "dr_a") and not approve(conn, draft, "dr_a"), "3: approved once"
    got = answer_for(conn, "P1", "after_extraction", "it", now)
    assert got and got[0]["text"]["approved_by"] == "dr_a" and "garza" in got[0]["text"]["body"], "3: the approved text"
    assert answer_for(conn, "P1", "after_filling", "it", now) is None, "3: the filling visit is older than 30 days"
    assert answer_for(conn, "P1", "after_extraction", "en", now)[0]["text"]["lang"] == "it", "3: the other language"
    assert answer_for(conn, "P2", "after_extraction", "it", now) is None, "3: another patient had no extraction"
    # 4. a new version: a draft until approved, then it replaces the old one, which is kept
    new, why = save_draft(conn, "extraction", "it", "Testo nuovo.", "dr_b")
    assert new and why is None and save_draft(conn, "extraction", "it", " ", "dr_b") == (None, "write the text")
    assert save_draft(conn, "bleaching", "it", "x", "dr_b") == (None, "unknown topic")
    assert "garza" in answer_for(conn, "P1", "after_extraction", "it", now)[0]["text"]["body"], "4: a draft replaced it"
    approve(conn, new, "dr_b")
    assert answer_for(conn, "P1", "after_extraction", "it", now)[0]["text"]["body"] == "Testo nuovo.", "4: the new one"
    assert conn.execute("SELECT status FROM aftercare_texts WHERE id = ?", (draft,)).fetchone()[0] == "retired", "4: kept"
    view = {r["topic"]: r for r in overview(conn)}
    assert view["extraction"]["it"]["approved"]["version"] == 2 and view["extraction"]["it"]["draft"] is None
    assert view["filling"]["en"]["draft"]["demo"] == 1 and view["filling"]["en"]["approved"] is None, "4: overview"
    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        print("usage: python aftercare.py --selftest")
