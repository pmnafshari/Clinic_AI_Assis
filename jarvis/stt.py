"""Speech to text for one question (J02), in a short-lived child process.

The model is P14's pinned faster-whisper small, CPU, int8, loaded only from its local folder with the Hugging Face client
told it is offline. It runs in a child because the model takes about 600 MB and its memory is not given back while the
process lives: a child per question keeps Jarvis itself small in READY (J01: <= 300 MB) and gives everything back when it
exits. The audio goes over a pipe and is never written anywhere. Italian or English only; a transcript under Whisper's
usual confidence floors is treated as not understood, never asked. Since J-D10 the clinic library's own terms come with
the audio, as a hint (one JSON line before the samples - never on the command line).

    python -m jarvis.stt < {"hint": [...]}\n + int16 mono 16 kHz samples      (the child; prints one JSON line)
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
MODEL_DIR = ROOT / "models" / "stt" / "faster-whisper-small@536b0662742c"    # models/stt/MANIFEST.json pins it
TIMEOUT = 20
NO_SPEECH = 0.6           # Whisper's own defaults for "nothing was said" and "not sure what was said"
LOW_LOGPROB = -1.0
LANGUAGES = ("it", "en")
BEAM = 5                  # Whisper's default; greedy (1) heard less on development audio (J02 follow-up 4)


class Unclear(Exception):
    """Nothing usable was heard: the question is not asked."""


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


def transcribe(pcm, hint=None, run=subprocess.run, timeout=TIMEOUT):
    """int16 samples (+ the library's terms) -> (text, language), or Unclear."""
    try:
        r = run([sys.executable, "-m", "jarvis.stt"], input=frame(pcm, hint), capture_output=True,
                timeout=timeout, cwd=ROOT)
    except subprocess.TimeoutExpired:
        raise Unclear("speech to text took too long") from None
    if r.returncode != 0:
        raise Unclear("speech to text is not available on this machine")
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
    model = WhisperModel(str(MODEL_DIR), device="cpu", compute_type="int8", cpu_threads=4, local_files_only=True)
    _lang, _p, probs = model.detect_language(pcm)
    language = pick_language(dict(probs))
    segments, _info = model.transcribe(pcm, language=language, beam_size=BEAM, temperature=0.0, vad_filter=False,
                                       condition_on_previous_text=False, without_timestamps=True, max_new_tokens=96,
                                       initial_prompt=prompt_for(hint))
    segments = list(segments)
    print(json.dumps({"text": " ".join(s.text for s in segments), "language": language,
                      "segments": [{"no_speech_prob": s.no_speech_prob, "avg_logprob": s.avg_logprob} for s in segments]}))


if __name__ == "__main__":
    child()
