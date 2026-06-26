import random

VOWELS = set('AEIOU')

_rng = random.Random(42)
_issued: set[str] = set()


def _get_consonants(name):
    return [c for c in name.upper() if c.isalpha() and c not in VOWELS]


def _get_vowels(name):
    return [c for c in name.upper() if c.isalpha() and c in VOWELS]


def _name_letters(name, count=2):
    consonants = _get_consonants(name)
    if len(consonants) >= count:
        return consonants[:count]
    result = consonants[:]
    result.extend(_get_vowels(name))
    while len(result) < count:
        result.append('X')
    return result[:count]


def make_cf(first_name, last_name):
    prefix = ''.join(_name_letters(first_name, 2) + _name_letters(last_name, 2))
    while True:
        digits = ''.join(str(_rng.randint(0, 9)) for _ in range(12))
        cf = prefix + digits
        if cf not in _issued:
            _issued.add(cf)
            return cf


def seed_cf(seed=42):
    _rng.seed(seed)
    _issued.clear()


def preload_issued(cfs):
    for cf in cfs:
        _issued.add(cf)
