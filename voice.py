"""Deepgram speech-to-text and ElevenLabs text-to-speech. Demo only.

Every entry point here refuses unless voice_config.status() says armed, so
the fence is not something a caller can forget - see voice_config.py for why
it exists.

Both calls are synchronous request/response, matching how the rest of this
project talks to a model (ask.py, agent.py, patient_app/chat.py all use
stream: False). Streaming transcription would need a websocket transport this
app does not have; that is recorded as missing rather than faked.
"""

import json
import urllib.error
import urllib.request

import voice_config

DEEPGRAM_URL = "https://api.deepgram.com/v1/listen"
ELEVENLABS_URL = "https://api.elevenlabs.io/v1/text-to-speech"
# deepgram's own TTS. preferred over elevenlabs because it reuses the key
# already validated against real audio, and because the elevenlabs free tier
# was measured exhausted (0 of 10000 characters, resets 2026-09-24).
AURA_URL = "https://api.deepgram.com/v1/speak"
AURA_VOICE = "aura-asteria-en"

# deepgram bills per second of audio and elevenlabs per character, so both
# calls are bounded. an unbounded upload from a browser is a cost incident.
MAX_AUDIO_BYTES = 5 * 1024 * 1024
MAX_TTS_CHARS = 800

TIMEOUT = 30


class VoiceUnavailable(RuntimeError):
    """Raised when the fence refuses, or a vendor call fails."""


def _require_armed(env=None, path=None):
    ok, why = voice_config.status(env=env, path=path)
    if not ok:
        raise VoiceUnavailable(why)
    return voice_config.keys(path)


def transcribe(audio_bytes, content_type="audio/webm", language="it",
               opener=urllib.request.urlopen, env=None, path=None):
    """Audio -> text. Returns '' when the vendor heard nothing.

    Never raises on empty speech: silence is a real outcome the UI has to
    render, not an error.
    """
    keys = _require_armed(env, path)
    if not audio_bytes:
        return ""
    if len(audio_bytes) > MAX_AUDIO_BYTES:
        raise VoiceUnavailable(
            f"audio is {len(audio_bytes)} bytes, over the {MAX_AUDIO_BYTES} cap")

    url = f"{DEEPGRAM_URL}?model=nova-2&smart_format=true&language={language}"
    req = urllib.request.Request(url, data=audio_bytes, method="POST", headers={
        "Authorization": f"Token {keys['DEEPGRAM_API_KEY']}",
        "Content-Type": content_type,
    })
    try:
        with opener(req, timeout=TIMEOUT) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        # the key must never reach a log or a page
        raise VoiceUnavailable(f"deepgram refused the request (HTTP {e.code})") from None
    except Exception as e:
        raise VoiceUnavailable(f"deepgram unreachable: {type(e).__name__}") from None

    try:
        alts = body["results"]["channels"][0]["alternatives"]
    except (KeyError, IndexError, TypeError):
        raise VoiceUnavailable("deepgram returned a shape this code does not understand") from None
    return (alts[0].get("transcript") or "").strip() if alts else ""


def speak(text, opener=urllib.request.urlopen, env=None, path=None):
    """Text -> mp3 bytes."""
    keys = _require_armed(env, path)
    text = (text or "").strip()
    if not text:
        raise VoiceUnavailable("nothing to speak")
    if len(text) > MAX_TTS_CHARS:
        text = text[:MAX_TTS_CHARS]

    url = f"{ELEVENLABS_URL}/{keys['ELEVENLABS_VOICE_ID']}"
    payload = json.dumps({
        "text": text,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
    }).encode()
    req = urllib.request.Request(url, data=payload, method="POST", headers={
        "xi-api-key": keys["ELEVENLABS_API_KEY"],
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    })
    try:
        with opener(req, timeout=TIMEOUT) as resp:
            audio = resp.read()
    except urllib.error.HTTPError as e:
        raise VoiceUnavailable(f"elevenlabs refused the request (HTTP {e.code})") from None
    except Exception as e:
        raise VoiceUnavailable(f"elevenlabs unreachable: {type(e).__name__}") from None
    if not audio:
        raise VoiceUnavailable("elevenlabs returned no audio")
    return audio


def speak_aura(text, voice=AURA_VOICE, opener=urllib.request.urlopen, env=None, path=None):
    """Text -> mp3 bytes, via Deepgram Aura. Same fence as everything here."""
    keys = _require_armed(env, path)
    text = (text or "").strip()
    if not text:
        raise VoiceUnavailable("nothing to speak")
    if len(text) > MAX_TTS_CHARS:
        text = text[:MAX_TTS_CHARS]

    req = urllib.request.Request(
        f"{AURA_URL}?model={voice}&encoding=mp3",
        data=json.dumps({"text": text}).encode(), method="POST",
        headers={"Authorization": f"Token {keys['DEEPGRAM_API_KEY']}",
                 "Content-Type": "application/json"})
    try:
        with opener(req, timeout=TIMEOUT) as resp:
            audio = resp.read()
    except urllib.error.HTTPError as e:
        raise VoiceUnavailable(f"deepgram tts refused the request (HTTP {e.code})") from None
    except Exception as e:
        raise VoiceUnavailable(f"deepgram tts unreachable: {type(e).__name__}") from None
    if not audio:
        raise VoiceUnavailable("deepgram tts returned no audio")
    return audio


def selftest():
    import io
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        good = Path(tmp) / "ok.voice"
        good.write_text("DEEPGRAM_API_KEY=k1\nELEVENLABS_API_KEY=k2\nELEVENLABS_VOICE_ID=v\n")
        armed = {"VOICE_DEMO": "1"}

        class Resp(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False

        # 1. the fence holds at the function boundary, not just in the UI.
        # a caller that forgets to check must still be refused.
        for fn, args in ((transcribe, (b"x",)), (speak, ("hello",)), (speak_aura, ("hello",))):
            try:
                fn(*args, env={}, path=good)
                raise AssertionError(f"1: {fn.__name__} ran with the demo flag off")
            except VoiceUnavailable as e:
                assert "off by default" in str(e), f"1: wrong refusal: {e}"

        # 2. and behind the tunnel, even when armed
        tunnelled = {"VOICE_DEMO": "1", "PATIENT_TRUST_FORWARDED_IP": "1"}
        try:
            transcribe(b"x", env=tunnelled, path=good)
            raise AssertionError("2: transcribe ran on the internet-facing surface")
        except VoiceUnavailable as e:
            assert "internet-facing" in str(e), f"2: wrong refusal: {e}"

        # 3. a transcript is returned, and silence is '' rather than an error
        body = {"results": {"channels": [{"alternatives": [{"transcript": " ciao "}]}]}}
        assert transcribe(b"a", opener=lambda r, timeout=0: Resp(json.dumps(body).encode()),
                          env=armed, path=good) == "ciao", "3: transcript not returned"
        empty = {"results": {"channels": [{"alternatives": [{"transcript": ""}]}]}}
        assert transcribe(b"a", opener=lambda r, timeout=0: Resp(json.dumps(empty).encode()),
                          env=armed, path=good) == "", "3: silence must be '', not an error"

        # 4. an unrecognised shape is a refusal, never a guess. inventing a
        # transcript is the one failure this module must not have.
        try:
            transcribe(b"a", opener=lambda r, timeout=0: Resp(b'{"results": {}}'),
                       env=armed, path=good)
            raise AssertionError("4: accepted a response shape it does not understand")
        except VoiceUnavailable as e:
            assert "does not understand" in str(e), f"4: wrong refusal: {e}"

        # 5. both calls are bounded - deepgram bills per second, elevenlabs
        # per character, and an unbounded browser upload is a cost incident
        try:
            transcribe(b"x" * (MAX_AUDIO_BYTES + 1), env=armed, path=good)
            raise AssertionError("5: accepted audio over the cap")
        except VoiceUnavailable as e:
            assert "over the" in str(e), f"5: wrong refusal: {e}"

        sent = {}
        def cap_opener(req, timeout=0):
            sent["len"] = len(json.loads(req.data)["text"])
            return Resp(b"MP3")
        speak("x" * (MAX_TTS_CHARS + 500), opener=cap_opener, env=armed, path=good)
        assert sent["len"] == MAX_TTS_CHARS, f"5: tts not truncated, sent {sent['len']}"

        # 6. no key value ever appears in an error. these reach logs.
        class Boom:
            def __call__(self, req, timeout=0):
                raise urllib.error.HTTPError(req.full_url, 401, "no", {}, None)
        for fn, args in ((transcribe, (b"a",)), (speak, ("hi",)), (speak_aura, ("hi",))):
            try:
                fn(*args, opener=Boom(), env=armed, path=good)
            except VoiceUnavailable as e:
                assert "k1" not in str(e) and "k2" not in str(e), f"6: a key leaked into {e!r}"

        # 7. audio is returned as bytes, not decoded
        assert speak("ciao", opener=lambda r, timeout=0: Resp(b"MP3"),
                     env=armed, path=good) == b"MP3", "7: audio must come back as bytes"
        assert speak_aura("ciao", opener=lambda r, timeout=0: Resp(b"MP3"),
                          env=armed, path=good) == b"MP3", "7: aura audio must come back as bytes"

        # 8. aura is capped like every other billed call
        sent2 = {}
        def cap2(req, timeout=0):
            sent2["len"] = len(json.loads(req.data)["text"]); return Resp(b"MP3")
        speak_aura("y" * (MAX_TTS_CHARS + 300), opener=cap2, env=armed, path=good)
        assert sent2["len"] == MAX_TTS_CHARS, f"8: aura not truncated, sent {sent2['len']}"

    print("selftest ok")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
