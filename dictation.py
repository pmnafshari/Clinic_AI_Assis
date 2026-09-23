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
import os
import re

from auth import authorize, log_audit

ENV_STT = "CLINIC_STT_ADAPTER"
ENV_TTS = "CLINIC_TTS_ADAPTER"
MAX_AUDIO_BYTES = 2 * 1024 * 1024
MAX_SPEAK_CHARS = 4000

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


class DisabledTTS:
    name = "disabled"

    def speak(self, text):
        raise Unavailable(TTS_UNAVAILABLE)


class SandboxTTS:
    name = "sandbox"

    def speak(self, text):
        return b"SANDBOX-AUDIO:" + str(len(text)).encode()


STT = {"disabled": DisabledSTT, "sandbox": SandboxSTT}
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
