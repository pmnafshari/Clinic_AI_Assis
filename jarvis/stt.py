"""Speech to text for one question (J02), in a short-lived child process.

The model is P14's pinned faster-whisper small, CPU, int8, loaded only from its local folder with the Hugging Face client
told it is offline. It runs in a child because the model takes about 600 MB and its memory is not given back while the
process lives: a child per question keeps Jarvis itself small in READY (J01: <= 300 MB) and gives everything back when it
exits. The audio goes over a pipe and is never written anywhere. Italian or English only; a transcript under Whisper's
usual confidence floors is treated as not understood, never asked. Since J-D10 the clinic library's own terms come with
the audio, as a hint (one JSON line before the samples - never on the command line).

Since J02 follow-up 6 a speech-to-text fault (no child, a crash, too slow) is Failed, not Unclear - it is not a hearing
problem; the child is killed as soon as its interaction is cancelled; and the hint is fitted, in the order the clinic app
gave it, to the prompt budget of the installed engine, counted with its own tokenizer (faster-whisper 1.2.1 keeps only
the last max_length // 2 - 1 = 223 prompt tokens, so an oversized prompt would lose its first - most important - terms).

    python -m jarvis.stt < {"hint": [...]}\n + int16 mono 16 kHz samples      (the child; prints one JSON line)
"""
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
MODEL_DIR = ROOT / "models" / "stt" / "faster-whisper-small@536b0662742c"    # models/stt/MANIFEST.json pins it
TIMEOUT = 20
NO_SPEECH = 0.6           # Whisper's own defaults for "nothing was said" and "not sure what was said"
LOW_LOGPROB = -1.0
LANGUAGES = ("it", "en")
BEAM = 5                  # Whisper's default; greedy (1) heard less on development audio (J02 follow-up 4)
PROMPT_BUDGET = 448 // 2 - 1          # faster-whisper 1.2.1: previous_tokens[-(self.max_length // 2 - 1):], max_length 448
POLL = 0.1                # how often a running child is checked for cancellation


class Unclear(Exception):
    """Nothing usable was heard: the question is not asked."""


class Failed(Exception):
    """Speech to text itself did not work (no child, a crash, too slow): the question is not asked."""


class Cancelled(Exception):
    """The interaction was cancelled while its speech was being worked out; the child has been stopped."""


def pick_language(probs):
    """The likelier of Italian and English, whatever else the detector thought."""
    return max(LANGUAGES, key=lambda lang: probs.get(lang, 0.0))


def frame(pcm, hint):
    return json.dumps({"hint": list(hint or [])}).encode() + b"\n" + np.asarray(pcm, np.int16).tobytes()


def read_request(data):
    """The child's stdin -> (int16 samples, hint terms)."""
    head, _, audio = data.partition(b"\n")
    return np.frombuffer(audio, np.int16), json.loads(head)["hint"]


def prompt_for(hint):
    """The library's terms as Whisper's prompt, or None without any."""
    return ", ".join(hint) + "." if hint else None


def token_counter():
    """The engine's own tokenizer -> a function giving how many prompt tokens a prompt costs (faster-whisper encodes
    " " + prompt.strip(), without special tokens)."""
    import tokenizers
    tok = tokenizers.Tokenizer.from_file(str(MODEL_DIR / "tokenizer.json"))
    return lambda prompt: len(tok.encode(" " + (prompt or "").strip(), add_special_tokens=False).ids)


def fit_hint(terms, count, budget=PROMPT_BUDGET):
    """The terms, in the order given, that fit the budget together: each is added if the prompt still fits, else
    skipped (a later, shorter one may still fit). The same terms always give the same result."""
    kept = []
    for term in terms:
        if count(prompt_for(kept + [term])) <= budget:
            kept.append(term)
    return kept


def run_child(cmd, data, timeout, cancelled, clock=time.monotonic):
    """Run the child with `data` on stdin; kill it as soon as `cancelled()` is true (Cancelled) or after `timeout`
    seconds (TimeoutExpired). Nothing is left running either way. Its pipes are served by threads: a communicate()
    retried after a timeout never resumes writing stdin, so a child that reads late would wait for ever."""
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=ROOT)
    out = {}

    def feed():
        try:
            p.stdin.write(data)
            p.stdin.close()
        except OSError:
            pass                               # the child has gone (killed, or it failed before reading)

    def drain(name, stream):
        out[name] = stream.read()
    pumps = [threading.Thread(target=feed, daemon=True),
             threading.Thread(target=drain, args=("stdout", p.stdout), daemon=True),
             threading.Thread(target=drain, args=("stderr", p.stderr), daemon=True)]
    for t in pumps:
        t.start()
    end = clock() + timeout
    try:
        while True:
            try:
                p.wait(timeout=POLL)
                break
            except subprocess.TimeoutExpired:
                pass
            if cancelled():
                raise Cancelled()
            if clock() >= end:
                raise subprocess.TimeoutExpired(cmd, timeout)
    finally:
        if p.poll() is None:
            p.kill()
            p.wait()
        for t in pumps:
            t.join(2)
        for stream in (p.stdin, p.stdout, p.stderr):
            try:
                stream.close()
            except OSError:
                pass
    return subprocess.CompletedProcess(cmd, p.returncode, out.get("stdout", b""), out.get("stderr", b""))


def transcribe(pcm, hint=None, run=None, timeout=TIMEOUT, cancelled=None):
    """int16 samples (+ the library's terms) -> (text, language); Unclear when nothing usable was heard, Failed when
    speech to text did not work, Cancelled when `cancelled()` turned true first."""
    cmd = [sys.executable, "-m", "jarvis.stt"]
    try:
        if run is None:
            r = run_child(cmd, frame(pcm, hint), timeout, cancelled or (lambda: False))
        else:
            r = run(cmd, input=frame(pcm, hint), capture_output=True, timeout=timeout, cwd=ROOT)
    except subprocess.TimeoutExpired:
        raise Failed(f"speech to text took longer than {timeout} s") from None
    except OSError as e:
        raise Failed(f"speech to text could not start ({type(e).__name__})") from None
    if r.returncode != 0:
        raise Failed("speech to text is not available on this machine")
    out = json.loads(r.stdout)
    text, segments = " ".join(out["text"].split()), out["segments"]
    if not text or not segments:
        raise Unclear("nothing could be made out")
    if all(s["no_speech_prob"] > NO_SPEECH for s in segments):
        raise Unclear("that did not sound like speech")
    if sum(s["avg_logprob"] for s in segments) / len(segments) < LOW_LOGPROB:
        raise Unclear("not sure what was said")
    return text, out["language"]


def child():
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    from faster_whisper import WhisperModel
    samples, hint = read_request(sys.stdin.buffer.read())
    pcm = samples.astype(np.float32) / 32768
    hint = fit_hint(hint, token_counter()) if hint else []
    model = WhisperModel(str(MODEL_DIR), device="cpu", compute_type="int8", cpu_threads=4, local_files_only=True)
    _lang, _p, probs = model.detect_language(pcm)
    language = pick_language(dict(probs))
    segments, _info = model.transcribe(pcm, language=language, beam_size=BEAM, temperature=0.0, vad_filter=False,
                                       condition_on_previous_text=False, without_timestamps=True, max_new_tokens=96,
                                       initial_prompt=prompt_for(hint))
    segments = list(segments)
    print(json.dumps({"text": " ".join(s.text for s in segments), "language": language, "hint_used": len(hint),
                      "segments": [{"no_speech_prob": s.no_speech_prob, "avg_logprob": s.avg_logprob} for s in segments]}))


if __name__ == "__main__":
    child()
