"""Avatar helpers for the staff and patient apps (phase 43).

There are no photos and none will be invented, so a person is drawn as
initials on a tint. The tint comes from the name, so the same person gets the
same colour on every page and in both apps.
"""


def initials(name):
    parts = [w for w in (name or "").replace("_", " ").split() if w]
    if not parts:
        return "?"
    return "".join(w[0] for w in parts[:2]).upper()


def tint(name):
    return f"ds-avatar-t{sum(map(ord, name or '')) % 4 + 1}"


def selftest():
    assert initials("Giulia Bianchi") == "GB", "1: two words give two letters"
    assert initials("zzv_dentist") == "ZD", "1: an underscore splits a username"
    assert initials("Anna") == "A", "1: one word gives one letter"
    assert initials("") == "?" and initials(None) == "?", "1: nothing gives a placeholder"
    assert initials("Maria del Carmen Rossi") == "MD", "1: never more than two letters"
    assert tint("Giulia Bianchi") == tint("Giulia Bianchi"), "2: a tint is stable"
    assert tint("x").startswith("ds-avatar-t") and tint("x")[-1] in "1234", "2: one of four tints"
    print("selftest ok")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        selftest()
