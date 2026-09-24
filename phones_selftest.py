"""P22: the phone contract. written before phones.py.

Italian numbers only (the clinic's patients); stored and shown in E.164
("+39..."); anything that is not a plausible Italian mobile or landline is not a
phone number and is not stored as one - "555 0000" was the case that showed it.
"""

import sys

import phones


def selftest():
    # 1. every way people write the same mobile number is one canonical value
    for raw in ("3478801234", "347 880 1234", "347-880-1234", "+39 347 880 1234", "0039 347 8801234",
                "(+39) 347.880.1234", "39 3478801234"):
        assert phones.canonical(raw) == "+393478801234", (raw, phones.canonical(raw))
    # landlines keep their leading zero after +39
    assert phones.canonical("06 1234 5678") == "+390612345678"
    assert phones.canonical("055 111") is None, "a six-digit landline is too short"
    assert phones.canonical("0551234567") == "+390551234567"

    # 2. not a phone number: never stored as one
    for raw in ("555 0000", "5550000", "12345", "333", "+44 20 7946 0000", "abc", "", None, "3" * 14,
                "+39 347 880 12345678"):
        assert phones.canonical(raw) is None, (raw, phones.canonical(raw))

    # 3. display is E.164 for a canonical value; an unusable stored value says so, never passes as a number
    assert phones.display("+393478801234") == "+393478801234"
    assert phones.display("3478801234") == "+393478801234"
    assert phones.display("555 0000") == "555 0000 (not a valid number)"
    assert phones.display(None) is None

    # 4. equality is by canonical value
    assert phones.same("347 880 1234", "+393478801234") and not phones.same("555 0000", "555 0000")
    print("selftest ok")


def main():
    if "--selftest" not in sys.argv:
        print("usage: python phones_selftest.py --selftest")
        return
    selftest()


if __name__ == "__main__":
    main()
