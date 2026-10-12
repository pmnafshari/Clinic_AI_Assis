"""J03 slice 2: reading "show me <patient>", "<patient>'s next appointment" and "change <patient>'s phone number to
<number>" out of what was heard. Fixed rules, no model. The name only proposes patients at the clinic; "her", "his" or
"their" means the patient Jarvis just handled. A bare "show me X" is not marked `explicit`: it may be a clinic-guide
question, and is treated as one unless a doctor is signed in and X is a patient.
"""
import re

from jarvis import notes

LEAD = r"^\s*(?:please\s+|(?:hey\s+)?jarvis[,\s]+|ok(?:ay)?[,\s]+)*"
END = r"\s*[.?!]*\s*$"
POSSESSIVE = r"(?:'s|’s|s'|’)"
NOUN = r"(?:record|file|chart|profile|notes)"
PHONE = r"(?:phone|telephone|mobile|cell)(?:\s+number)?"
OPEN = [
    (re.compile(LEAD + r"(?:show(?:\s+me)?|open(?:\s+up)?|pull\s+up|bring\s+up)\s+(?:the\s+|a\s+)?" + NOUN +
                r"\s+(?:of|for)\s+(?:the\s+)?(?:patient\s+)?(?P<name>.+?)" + END, re.I), True),
    (re.compile(LEAD + r"(?:show(?:\s+me)?|open(?:\s+up)?|pull\s+up|bring\s+up)\s+(?:the\s+)?(?:patient\s+)?(?P<name>.+?)"
                + POSSESSIVE + r"?\s+" + NOUN + END, re.I), True),
    (re.compile(LEAD + r"(?:show\s+me|open(?:\s+up)?|pull\s+up|bring\s+up)\s+(?:patient\s+)?(?P<name>.+?)" + END, re.I), False),
]
NEXT = [
    re.compile(LEAD + r"(?:when|what)(?:'s|’s|\s+is)\s+(?:the\s+)?next\s+appointment\s+(?:for|of)\s+(?:the\s+)?(?:patient\s+)?"
               r"(?P<name>.+?)" + END, re.I),
    re.compile(LEAD + r"(?:when|what)(?:'s|’s|\s+is)\s+(?P<name>.+?)" + POSSESSIVE + r"?\s+next\s+appointment" + END, re.I),
    re.compile(LEAD + r"(?:show\s+me\s+|tell\s+me\s+)?(?:the\s+)?next\s+appointment\s+(?:for|of)\s+(?:the\s+)?(?:patient\s+)?"
               r"(?P<name>.+?)" + END, re.I),
    re.compile(LEAD + r"(?:show\s+me\s+|tell\s+me\s+)?(?P<name>.+?)" + POSSESSIVE + r"?\s+next\s+appointment" + END, re.I),
]
EDIT = [
    re.compile(LEAD + r"(?:change|set|update|make)\s+(?:the\s+)?" + PHONE + r"\s+(?:of|for)\s+(?:the\s+)?(?:patient\s+)?"
               r"(?P<name>.+?)\s+(?:to|as|into)\s+(?P<value>.+?)" + END, re.I),
    re.compile(LEAD + r"(?:change|set|update|make)\s+(?:the\s+patient\s+)?(?P<name>.+?)" + POSSESSIVE + r"?\s+" + PHONE +
               r"\s+(?:to|as|into)\s+(?P<value>.+?)" + END, re.I),
]
PRONOUNS = {"her", "his", "their", "him", "them", "the patient", "this patient", "that patient", "the patient's",
            "this patient's", "that patient's"}
DIGITS = {"zero": "0", "oh": "0", "o": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
          "seven": "7", "eight": "8", "nine": "9", "plus": "+"}
MAX_NAME_WORDS = 5
# a "name" starting with one of these is part of a question, not a patient: "show me how ...", "how do I book the next
# appointment"
STOP = {"how", "what", "where", "which", "when", "why", "who", "the", "a", "an", "to", "me", "my", "your", "our", "all",
        "today", "today's", "this", "that", "these", "those", "it", "its", "i", "we", "you", "do", "does", "can", "is",
        "are", "settings", "setting", "button", "program", "manual", "guide", "page"}


def name_of(raw):
    """-> the spoken name with any spelled-out word applied; None for a pronoun (the patient just handled); "" when it
    is not a name that can be looked up safely."""
    raw = (raw or "").strip(" .,;:!?'’")
    if raw.casefold() in PRONOUNS:
        return None
    spelled = {}
    for s in notes.SPELLED.finditer(raw):       # "Julia G-I-U-L-I-A Santoro": the spelling wins over the word heard
        word = re.sub(r"[-\s.]", "", s.group("letters")).capitalize()
        spelled[(s.group("said") or word).casefold()] = word
    raw = notes.SPELLED.sub(lambda s: " " + re.sub(r"[-\s.]", "", s.group("letters")).capitalize() + " ", raw)
    raw = re.sub(r"^(?:the\s+)?patient\s+", "", raw.strip(), flags=re.I).strip(" .,;:!?'’")
    words = [spelled.get(w.casefold(), w) for w in raw.split()]
    if not words or len(words) > MAX_NAME_WORDS or not all(re.fullmatch(r"[^\W\d_]+(?:['’-][^\W\d_]+)*", w) for w in words):
        return ""
    if any(w.casefold() in STOP for w in words):
        return ""
    return " ".join(words)


def number_of(raw):
    """A spoken phone number as digits: "three three three, one two three..." or "333 123 4567" -> "3331234567";
    anything that is not a number is passed on as heard (the clinic refuses it)."""
    out, repeat = [], 1
    for w in re.findall(r"\+|\d+|[a-z]+", (raw or "").casefold()):
        if w in ("double", "triple"):
            repeat = 2 if w == "double" else 3
            continue
        if w in ("and", "dash", "space"):
            continue
        d = w if (w.isdigit() or w == "+") else DIGITS.get(w)
        if d is None:
            return (raw or "").strip()
        out.append(d * repeat if len(d) == 1 else d)
        repeat = 1
    return "".join(out)


def parse(heard):
    """-> {kind: open|next|edit, name, explicit[, field, value]} for a patient command (name None = the patient just
    handled); None for anything else, including a command whose name cannot be looked up safely."""
    heard = heard or ""
    found = None
    for rx in EDIT:
        m = rx.match(heard)
        if m:
            found = {"kind": "edit", "name": m.group("name"), "explicit": True, "field": "phone",
                     "value": number_of(m.group("value"))}
            break
    for rx in NEXT if found is None else ():
        m = rx.match(heard)
        if m:
            found = {"kind": "next", "name": m.group("name"), "explicit": True}
            break
    for rx, explicit in OPEN if found is None else ():
        m = rx.match(heard)
        if m:
            found = {"kind": "open", "name": m.group("name"), "explicit": explicit}
            break
    if found is None:
        return None
    found["name"] = name_of(found["name"])
    return None if found["name"] == "" else found
