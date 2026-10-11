"""J03, first slice: reading "add a note to <patient> saying <words>" out of what was heard. Fixed rules, no model:
a command it does not recognise is not a note, and a note command it cannot split is sent back to be said again - it
never guesses the patient or the words.
"""
import re

COMMAND = re.compile(r"^\s*(?:please\s+|(?:hey\s+)?jarvis[,\s]+)*(?:add|make|write|put|leave|create)\s+"
                     r"(?:a\s+|an\s+)?(?:new\s+)?note\s+(?:to|for|on|in|into|about)\s+(?:the\s+)?(?:patient\s+)?(?P<rest>.+?)\s*$", re.I)
SPLIT = re.compile(r"\s*(?:[,:;.]\s*)?\b(?:saying|that says|that)\b\s*|\s*(?:[,:;]|\.\s)\s*", re.I)
# a word spelled out after it, as people do with a name: "Julia G-I-U-L-I-A" or "Julia G I U L I A"
SPELLED = re.compile(r"(?:\b(?P<said>[^\W\d_]+)\s+)?(?P<letters>\b[A-Za-z](?:[-\s.][A-Za-z]){3,}\b)\.?")
RECORD = re.compile(r"(?:'s|’s|s')?\s*(?:record|profile|file|chart|notes)?\s*$", re.I)
MAX_NAME_WORDS = 5


def parse(heard):
    """-> (patient, words) for a note command; (None, None) for a note command that cannot be split safely; None
    for anything else."""
    m = COMMAND.match(heard or "")
    if not m:
        return None
    rest, spelled = m.group("rest"), {}
    for s in SPELLED.finditer(rest):          # the spelling wins over the word heard before it
        word = re.sub(r"[-\s.]", "", s.group("letters")).capitalize()
        spelled[(s.group("said") or word).casefold()] = word
    rest = SPELLED.sub("", rest).strip()
    parts = SPLIT.split(rest, maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        return None, None
    name = RECORD.sub("", parts[0]).strip(" .,'’")
    name = " ".join(spelled.get(w.casefold(), w) for w in name.split())
    words = re.sub(r"\s{2,}", " ", parts[1]).strip()
    if not name or len(name.split()) > MAX_NAME_WORDS or not re.search(r"[^\W\d_]", name):
        return None, None
    return name, words
