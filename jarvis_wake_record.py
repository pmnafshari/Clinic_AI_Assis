"""Record the clinic's own voices saying the wake word, to train Nana's wake model on how the clinic says it.

The synthetic training voice is English; Rima, Nana and Ava all failed on Italian speakers (2026-11-09). Real takes
from consenting staff are what the training lacks. Each person gets a code (S01, S02...), never a name; nothing
leaves this computer and nothing goes into the repository - the folder must be outside it. A person's takes are
deleted with --delete CODE at any time.

    .venv/bin/python jarvis_wake_record.py --phrase nana --speaker S01 [--takes 30]
    .venv/bin/python jarvis_wake_record.py --phrase nana --delete S01
    .venv/bin/python jarvis_wake_record.py --list
"""
import json
import shutil
import sys
import time
import wave
from pathlib import Path

import numpy as np

import clinic_time

RATE = 16000
SECONDS = 2.0                  # one take: the word, said after the beep, with room before and after
ROOT = Path(__file__).resolve().parent
OUT = ROOT.parent / "jarvis-training" / "real"      # beside the LibriSpeech cache, outside the repository
QUIET, LOUD = 300, 32000       # a take's peak below QUIET heard nothing; above LOUD it clipped
CONSENT = (
    "Your voice saying the wake word is recorded on this computer only, to teach Nana to recognise it.\n"
    "It is never sent anywhere and never put in the code repository. You can have your recordings deleted\n"
    "at any time (jarvis_wake_record.py --delete <your code>).\n"
    "La tua voce che dice la parola di attivazione viene registrata solo su questo computer, per insegnare\n"
    "a Nana a riconoscerla. Non viene mai inviata altrove né messa nel repository del codice. Puoi far\n"
    "cancellare le tue registrazioni in qualsiasi momento (jarvis_wake_record.py --delete <il tuo codice>).\n"
    "Do you agree? / Sei d'accordo? (yes/sì): ")


def mic(seconds):
    """seconds of audio from the default microphone -> int16, 16 kHz mono"""
    import sounddevice as sd
    data = sd.rec(int(seconds * RATE), samplerate=RATE, channels=1, dtype="int16")
    sd.wait()
    return data.reshape(-1)


def beep():
    sys.stdout.write("\a")
    sys.stdout.flush()


def check(audio):
    """None when the take is usable, else why not"""
    peak = int(np.abs(audio.astype(np.int32)).max()) if len(audio) else 0
    if peak < QUIET:
        return "too quiet - speak a little louder or closer"
    if peak >= LOUD:
        return "too loud - it clipped; a little further from the microphone"
    return None


def write_wav(path, audio):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(audio.astype(np.int16).tobytes())


def folder(out, phrase, speaker):
    out = Path(out).resolve()
    if out == ROOT or ROOT in out.parents:
        raise SystemExit("refused: recordings never go inside the repository")
    if not speaker or not speaker.isalnum() or len(speaker) > 8:
        raise SystemExit("the speaker is a short code like S01, never a name")
    return out / phrase.lower() / speaker.upper()


def record(phrase, speaker, takes=30, out=OUT, ask=input, listen=mic, say=print, signal=beep):
    """-> the number of takes kept. Asks for consent first; redoes a take that is too quiet or clipped."""
    where = folder(out, phrase, speaker)
    if ask(CONSENT).strip().lower() not in ("yes", "y", "si", "sì"):
        say("No consent - nothing recorded.")
        return 0
    where.mkdir(parents=True, exist_ok=True)
    kept = len(list(where.glob("take-*.wav")))
    say(f'Say "{phrase}" once after each beep, as you would to Nana. {takes} takes.')
    tries = 0
    while kept < takes and tries < takes * 3:
        tries += 1
        say(f"Take {kept + 1}/{takes}...")
        time.sleep(0.4)
        signal()
        audio = listen(SECONDS)
        why = check(audio)
        if why:
            say(f"  again: {why}")
            continue
        kept += 1
        write_wav(where / f"take-{kept:02d}.wav", audio)
    meta = {"speaker": speaker.upper(), "phrase": phrase.lower(), "takes": kept, "rate": RATE,
            "consented_at": clinic_time.to_storage(clinic_time.now_utc())}
    (where / "consent.json").write_text(json.dumps(meta, indent=1))
    say(f"Kept {kept} takes in {where}")
    return kept


def delete(phrase, speaker, out=OUT):
    where = folder(out, phrase, speaker)
    if where.exists():
        shutil.rmtree(where)
        return True
    return False


def listing(out=OUT):
    """[(phrase, speaker, takes)]"""
    rows = []
    for meta in sorted(Path(out).glob("*/*/consent.json")):
        m = json.loads(meta.read_text())
        rows.append((m["phrase"], m["speaker"], len(list(meta.parent.glob("take-*.wav")))))
    return rows


def selftest():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "real"
        said = []
        rng = np.random.default_rng(3)
        word = (np.sin(np.linspace(0, 400, int(SECONDS * RATE))) * 8000).astype(np.int16)
        quiet = (rng.standard_normal(int(SECONDS * RATE)) * 20).astype(np.int16)
        loud = np.full(int(SECONDS * RATE), 32767, np.int16)
        feed = [quiet, word, loud, word, word]

        # 1. no consent, nothing recorded
        n = record("nana", "s01", 3, out, ask=lambda q: "no", listen=lambda s: feed.pop(0), say=said.append,
                   signal=lambda: None)
        assert n == 0 and not out.exists(), "1: HARD FAIL - recorded without consent"
        assert "Nana" in CONSENT and "sì" in CONSENT, "1: the consent is asked in both languages"

        # 2. consent: three good takes kept, a quiet one and a clipped one redone; the consent kept beside them
        n = record("Nana", "s01", 3, out, ask=lambda q: "sì", listen=lambda s: feed.pop(0), say=said.append,
                   signal=lambda: None)
        where = out / "nana" / "S01"
        assert n == 3 and sorted(p.name for p in where.glob("take-*.wav")) == ["take-01.wav", "take-02.wav", "take-03.wav"]
        assert any("too quiet" in s for s in said) and any("clipped" in s for s in said), "2: bad takes not redone"
        with wave.open(str(where / "take-01.wav")) as w:
            assert (w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()) == (RATE, 1, 2, len(word))
        meta = json.loads((where / "consent.json").read_text())
        assert meta["speaker"] == "S01" and meta["takes"] == 3 and meta["consented_at"], "2: the consent record"
        assert listing(out) == [("nana", "S01", 3)], "2: the listing"

        # 3. a name or a path is refused as a speaker; the repository is refused as a place
        for bad in ("Mario Rossi", "../x", "", "S01234567"):
            try:
                folder(out, "nana", bad)
                raise AssertionError(f"3: {bad!r} accepted as a speaker code")
            except SystemExit:
                pass
        try:
            folder(ROOT / "recordings", "nana", "S02")
            raise AssertionError("3: HARD FAIL - recordings inside the repository")
        except SystemExit:
            pass
        assert ROOT not in OUT.resolve().parents, "3: the default place is inside the repository"

        # 4. deleted on request
        assert delete("nana", "s01", out) and not where.exists() and listing(out) == [], "4: not deleted"
        assert not delete("nana", "s01", out), "4: deleted twice"
    print("selftest ok")


def main(argv):
    if "--selftest" in argv:
        return selftest()
    if "--list" in argv:
        for row in listing():
            print("%s  %s  %d takes" % row)
        return
    def arg(name, default=None):
        return argv[argv.index(name) + 1] if name in argv else default
    phrase = arg("--phrase")
    if not phrase:
        print(__doc__)
        return
    if "--delete" in argv:
        print("deleted" if delete(phrase, arg("--delete")) else "nothing to delete")
        return
    record(phrase, arg("--speaker", ""), int(arg("--takes", "30")))


if __name__ == "__main__":
    main(sys.argv[1:])
