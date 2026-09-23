"""Clinical dictation and read-aloud (P14). The machinery, not the engines.

THERE IS NO SPEECH ENGINE ON THIS MACHINE. No local speech-to-text model is
installed, and none can be without a download, a licence review and disk space
the machine does not have (P14.01 BLOCKED). No local text-to-speech engine is
approved for clinical text (P14.05 BLOCKED). So both registries hold `disabled`
- the default - and `sandbox`, which tests use. The cloud voice demo in
voice.py is for the public site only and is not reachable from here: clinical
speech to Deepgram or ElevenLabs needs D01 and D08, and naming either engine
resolves to disabled.

WHAT DICTATION IS. Text in the typed-note form, for the locked patient, before
extraction. The note is saved only by the existing confirm step (P13): nothing
here writes a note, and nothing here stores audio. A dictated command only
fills the agent's command box; its allowlist and confirm step are unchanged.

WHAT MAY BE READ ALOUD. Only the current approved, unchanged next-visit summary
of the patient on screen - decided here, not in the page - and only after the
clinician confirms they are somewhere private. Nothing plays by itself.
"""
import io
import os
import re
import struct
import threading
import time
import wave
from pathlib import Path

from auth import authorize, log_audit

ENV_STT = "CLINIC_STT_ADAPTER"
ENV_TTS = "CLINIC_TTS_ADAPTER"
MAX_AUDIO_BYTES = 2 * 1024 * 1024
MAX_AUDIO_SECONDS = 60
MAX_SPEAK_CHARS = 4000
RATE_LIMIT = 20            # dictations per user
RATE_WINDOW = 600          # seconds

# the one local model, pinned by revision (models/stt/MANIFEST.json holds its
# sha256). loaded only from this folder; nothing is ever downloaded at runtime
STT_MODEL_DIR = (Path(__file__).resolve().parent / "models" / "stt"
                 / "faster-whisper-small@536b0662742c")

STT_UNAVAILABLE = ("Dictation is not available: there is no approved local speech model on this"
                   " machine. Please type the note.")
TTS_UNAVAILABLE = ("Reading aloud is not available: there is no approved local voice on this"
                   " machine.")


class Unavailable(RuntimeError):
    """The engine is off, missing, or refused the input. Say why; offer typing."""


class Unclear(ValueError):
    """Audio arrived but nothing could be made out. An alert, never a note."""


class NotPrivate(PermissionError):
    """Reading aloud needs the clinician to confirm the room is private."""


class DisabledSTT:
    name = "disabled"

    def transcribe(self, audio, lang):
        raise Unavailable(STT_UNAVAILABLE)


class SandboxSTT:
    """Tests only. "Hears" the upload's bytes as text; talks to nothing."""
    name = "sandbox"

    def transcribe(self, audio, lang):
        return audio.decode("utf-8", errors="replace")


def decode_wav(audio):
    """Bytes -> 16 kHz mono float samples. WAV (RIFF/WAVE, 16-bit PCM) only,
    judged by the bytes themselves - a filename or a declared type is ignored."""
    import numpy as np
    if len(audio) < 44 or audio[:4] != b"RIFF" or audio[8:12] != b"WAVE":
        raise Unavailable("Only WAV recordings are accepted.")
    try:
        with wave.open(io.BytesIO(audio)) as w:
            channels, width, rate, frames = (w.getnchannels(), w.getsampwidth(),
                                             w.getframerate(), w.getnframes())
            if width != 2 or channels not in (1, 2) or not 8000 <= rate <= 48000:
                raise Unavailable("The recording must be 16-bit PCM, mono or stereo.")
            if frames / rate > MAX_AUDIO_SECONDS:
                raise Unavailable("The recording is too long. Record a shorter part, or type it.")
            raw = w.readframes(frames)
    except (wave.Error, EOFError, struct.error):
        raise Unavailable("The recording could not be read.") from None
    pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768
    if channels == 2:
        pcm = pcm.reshape(-1, 2).mean(axis=1)
    if rate != 16000:
        target = int(len(pcm) * 16000 / rate)
        pcm = np.interp(np.linspace(0, len(pcm) - 1, target), np.arange(len(pcm)), pcm)
    return pcm.astype(np.float32)


_RATE = {}
_RATE_LOCK = threading.Lock()


def check_rate(username):
    """At most RATE_LIMIT dictations per user per RATE_WINDOW seconds."""
    now = time.monotonic()
    with _RATE_LOCK:
        recent = [t for t in _RATE.get(username, []) if now - t < RATE_WINDOW]
        if len(recent) >= RATE_LIMIT:
            raise Unavailable("Too many recordings in a short time. Wait a few minutes, or type it.")
        recent.append(now)
        _RATE[username] = recent


_LOCAL_MODEL = {}
_LOAD_LOCK = threading.Lock()
_RUN_LOCK = threading.Lock()


class LocalWhisperSTT:
    """faster-whisper small, CPU, int8, from the pinned local folder only.

    Offline by construction: the Hugging Face client is told it is offline
    before the library is imported, the model is loaded from a path with
    local_files_only, and a missing folder is an error - never a download.
    Audio is decoded in memory from WAV; nothing is written to disk."""
    name = "local"

    def _model(self):
        with _LOAD_LOCK:
            if "m" not in _LOCAL_MODEL:
                if not (STT_MODEL_DIR / "model.bin").exists():
                    raise Unavailable("Dictation is not available: the local speech model is not"
                                      " installed on this machine. Please type the note.")
                os.environ["HF_HUB_OFFLINE"] = "1"
                os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
                from faster_whisper import WhisperModel
                _LOCAL_MODEL["m"] = WhisperModel(str(STT_MODEL_DIR), device="cpu",
                                                 compute_type="int8", cpu_threads=4,
                                                 local_files_only=True)
            return _LOCAL_MODEL["m"]

    def transcribe(self, audio, lang):
        pcm = decode_wav(audio)
        model = self._model()
        # one transcription at a time: a 16 GB machine, and a model loaded once
        with _RUN_LOCK:
            segments, _info = model.transcribe(pcm, language=lang, beam_size=1,
                                               vad_filter=False, condition_on_previous_text=False)
            return " ".join(seg.text for seg in segments).strip()


class DisabledTTS:
    name = "disabled"

    def speak(self, text):
        raise Unavailable(TTS_UNAVAILABLE)


class SandboxTTS:
    name = "sandbox"

    def speak(self, text):
        return b"SANDBOX-AUDIO:" + str(len(text)).encode()


STT = {"disabled": DisabledSTT, "sandbox": SandboxSTT, "local": LocalWhisperSTT}
TTS = {"disabled": DisabledTTS, "sandbox": SandboxTTS}


def _pick(registry, value, env):
    name = (value or "disabled").strip().lower()
    # an unknown name - a typo, or a cloud engine someone tried to switch on -
    # is disabled. so is the sandbox in production.
    if name not in registry:
        return registry["disabled"]()
    if name == "sandbox" and (env.get("CLINIC_ENV") or "").strip().lower() == "production":
        return registry["disabled"]()
    return registry[name]()


def stt_adapter(env=None):
    env = os.environ if env is None else env
    return _pick(STT, env.get(ENV_STT), env)


def tts_adapter(env=None):
    env = os.environ if env is None else env
    return _pick(TTS, env.get(ENV_TTS), env)


def stt_status(env=None):
    adapter = stt_adapter(env)
    return adapter.name != "disabled", ("" if adapter.name != "disabled" else STT_UNAVAILABLE)


def tts_status(env=None):
    adapter = tts_adapter(env)
    return adapter.name != "disabled", ("" if adapter.name != "disabled" else TTS_UNAVAILABLE)


def transcribe(audio, lang, env=None):
    """Audio -> text, in memory. The bytes are not kept by anything here."""
    adapter = stt_adapter(env)
    if adapter.name == "disabled":
        raise Unavailable(STT_UNAVAILABLE)
    if len(audio) > MAX_AUDIO_BYTES:
        raise Unavailable("The recording is too long. Record a shorter part, or type it.")
    text = adapter.transcribe(audio, lang if lang in ("it", "en") else "it").strip()
    if not text:
        raise Unclear("The recording was unclear: could not make out what was said. Try again"
                      " closer to the microphone, or type it.")
    return text


def readable_summary(conn, pid, actor, role):
    """The text that may be read aloud for this patient, or None.

    Exactly what a patient's export would carry (POL-10): the current approved
    summary, unchanged since approval, every line supported. Never a draft.
    """
    import visit_summary
    if not authorize(role, visit_summary.CAPABILITY):
        log_audit(conn, actor, role, "summary_read_aloud", f"patient:{pid}", allowed=0)
        raise PermissionError(f"{role} may not have a summary read aloud")
    out = visit_summary.exportable(conn, pid)
    if out is None:
        return None
    # the [#visit] references are for reading on screen, not for saying aloud
    return "\n".join(re.sub(r"\s*\[#\d+\]", "", line) for line in out["lines"])[:MAX_SPEAK_CHARS]


def speak(text, private_confirmed, env=None):
    if not private_confirmed:
        raise NotPrivate("Confirm you are somewhere private before anything is read aloud.")
    adapter = tts_adapter(env)
    if adapter.name == "disabled":
        raise Unavailable(TTS_UNAVAILABLE)
    return adapter.speak(text[:MAX_SPEAK_CHARS])
