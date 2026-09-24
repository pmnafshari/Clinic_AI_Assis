"""the phone contract (P22). the clinic's patients are in Italy, so a phone number
here is an Italian mobile (3 + 8-9 more digits, 9-10 in all) or landline (0 + 8-10
more digits, 9-11 in all), written any usual way, with or without +39 / 0039.

canonical() returns E.164 ("+39..."), the one stored form; anything else returns
None and is not stored as a phone number. display() shows E.164, and marks a
legacy stored value that is not a valid number instead of passing it off as one.
"""

import re

_SEPARATORS = re.compile(r"[\s().\-/]")


def canonical(value):
    if not value or not isinstance(value, str):
        return None
    digits = _SEPARATORS.sub("", value.strip())
    if digits.startswith("+39"):
        digits = digits[3:]
    elif digits.startswith("0039"):
        digits = digits[4:]
    elif digits.startswith("39") and len(digits) in (11, 12) and digits[2] in "03":
        digits = digits[2:]
    if not digits.isdigit():
        return None
    if digits[0] == "3" and 9 <= len(digits) <= 10:
        return "+39" + digits
    if digits[0] == "0" and 9 <= len(digits) <= 11:
        return "+39" + digits
    return None


def display(value):
    if value is None:
        return None
    good = canonical(value)
    return good if good else f"{value} (not a valid number)"


def same(a, b):
    ca, cb = canonical(a), canonical(b)
    return ca is not None and ca == cb


def selftest():
    import phones_selftest
    phones_selftest.selftest()


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        selftest()
