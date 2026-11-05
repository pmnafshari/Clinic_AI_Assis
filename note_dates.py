"""P29 U-003: the dates a visit note writes, so a note that says another day than its visit's is flagged - never silently
resolved one way or the other. "27 lug" on a visit stored as 2023-08-27 is a question for a person, not an answer.

Read: 27/07, 27-07-2023, 27.07.23, "27 lug", "27 luglio", "27 July", "July 27", "Jul 27". A date the note gives as the
next visit (prossimo controllo 12/09, recall 12/09, next 12/09) is a recall, not the visit's own day, and is left out.
"""
import re
import sys

MONTHS = {}
for number, names in enumerate((
        ("gen", "genn", "gennaio", "jan", "january"), ("feb", "febbraio", "february"), ("mar", "marzo", "march"),
        ("apr", "aprile", "april"), ("mag", "maggio", "may"), ("giu", "giugno", "jun", "june"),
        ("lug", "luglio", "jul", "july"), ("ago", "agosto", "aug", "august"),
        ("set", "sett", "settembre", "sep", "sept", "september"), ("ott", "ottobre", "oct", "october"),
        ("nov", "novembre", "november"), ("dic", "dicembre", "dec", "december")), start=1):
    for name in names:
        MONTHS[name] = number

_NAME = "|".join(sorted(MONTHS, key=len, reverse=True))
NUMERIC = re.compile(r"\b(\d{1,2})[/.-](\d{1,2})(?:[/.-](\d{2,4}))?\b")
DAY_MONTH = re.compile(rf"\b(\d{{1,2}})\s+({_NAME})\.?\b", re.IGNORECASE)
MONTH_DAY = re.compile(rf"\b({_NAME})\.?\s+(\d{{1,2}})\b", re.IGNORECASE)
RECALL = re.compile(r"(prossim\w*|richiam\w*|recall|next|follow[- ]?up|entro|torna\w*|rivedere|rivediamo|fu)\W+(\w+\W+){0,4}$",
                    re.IGNORECASE)


def mentioned(text):
    """[(as written, day, month)] - every date the note writes for the day itself, recall dates left out"""
    text = text or ""
    found = []
    for rx, day_at, month_at in ((NUMERIC, 1, 2), (DAY_MONTH, 1, 2), (MONTH_DAY, 2, 1)):
        for m in rx.finditer(text):
            if RECALL.search(text[max(0, m.start() - 40):m.start()]):
                continue
            day, month = m.group(day_at), m.group(month_at)
            month = int(month) if month.isdigit() else MONTHS[month.lower()]
            day = int(day)
            if 1 <= day <= 31 and 1 <= month <= 12:
                found.append((m.group(0), day, month))
    return found


def differs(visit_date, text):
    """what the note writes as its date when that is not the visit's own day, else None. a note that writes the
    visit's day anywhere agrees with it"""
    if not visit_date:
        return None
    said = mentioned(text)
    day, month = int(visit_date[8:10]), int(visit_date[5:7])
    if not said or any((d, m) == (day, month) for _w, d, m in said):
        return None
    return said[0][0]


def selftest():
    assert differs("2023-08-27", "controllo 27 lug, tutto ok") == "27 lug", "1: the UAT case"
    for note in ("controllo 27/08", "27 agosto: igiene", "check-up on Aug 27", "nessuna data", "igiene 27.08.23",
                 "27/08 igiene, prossimo controllo 12/09", "ok, richiamo tra 6 mesi, 03/03", "ok, next 12 sep"):
        assert differs("2023-08-27", note) is None, f"2: {note!r}"
    assert differs("2023-08-27", "visita del 26/08/2023") == "26/08/2023", "3: a day off"
    assert differs("2023-08-27", "July 27, ok") == "July 27", "3: a month off"
    assert differs(None, "27 lug") is None, "4: an undated visit has nothing to differ from"
    assert mentioned("dente 46, 47 e 48") == [], "5: tooth numbers are not dates"
    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        print("usage: python note_dates.py --selftest")
