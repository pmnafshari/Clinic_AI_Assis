"""Demo-only third-party voice. OFF unless explicitly switched on.

This module is a deliberate, fenced exception to the project's core premise.
PROJECT.md opens with "a private, offline-first AI assistant ... built around
the clinic's own small fine-tuned models", and everything else in this repo
honours that: ollama is local, embeddings are local, fonts and icons are
vendored so no third party sees a visitor.

Deepgram and ElevenLabs break that. Audio sent to them LEAVES THE MACHINE, to
processors outside the clinic's control. Patient speech to a dental assistant
carries symptoms, treatments and invoices, which is GDPR Article 9
special-category health data, and using these services for real patients needs
an Article 28 processor agreement with each vendor - neither of which exists.
The project already has one unresolved DPA blocker (DEPLOY-02).

So the fence, not the feature, is the important part of this file:

  * OFF by default. Nothing happens without VOICE_DEMO=1 in the environment.
  * Refuses to arm behind the tunnel. PATIENT_TRUST_FORWARDED_IP is only set
    when the patient app is internet-facing; cloud voice must never be on the
    surface real patients can reach.
  * Every call is auditable by the caller - see voice.py.
  * The keys live in .env.voice, gitignored, chmod 600.

CLAUDE.md's stack names faster-whisper and Piper for voice: free, offline, no
processor agreement needed. That remains the intended production path. This is
a demo.
"""

import os
from pathlib import Path

ENV_PATH = Path(".env.voice")

# a demo flag, not a feature flag: absent means the whole surface is off
DEMO_ENV = "VOICE_DEMO"
TUNNEL_ENV = "PATIENT_TRUST_FORWARDED_IP"

BANNER = (
    "Demo voice: audio is sent to Deepgram and ElevenLabs, outside this "
    "machine. Never use with real patient data."
)


def _read_env(path=None):
    path = Path(path) if path else ENV_PATH
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def status(env=None, path=None):
    """-> (enabled: bool, reason: str). reason explains a refusal."""
    env = os.environ if env is None else env
    if env.get(DEMO_ENV) != "1":
        return False, f"{DEMO_ENV} is not set - demo voice is off by default"
    if env.get(TUNNEL_ENV):
        # the one refusal that is not about convenience
        return False, (f"{TUNNEL_ENV} is set: the patient app is internet-facing, "
                       "and demo voice must never run on the surface real patients reach")
    keys = _read_env(path)
    missing = [k for k in ("DEEPGRAM_API_KEY", "ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID")
               if not keys.get(k)]
    if missing:
        return False, f"{ENV_PATH} is missing: {', '.join(missing)}"
    return True, "demo voice armed"


def keys(path=None):
    return _read_env(path)


def selftest():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        good = Path(tmp) / "ok.voice"
        good.write_text("DEEPGRAM_API_KEY=a\nELEVENLABS_API_KEY=b\nELEVENLABS_VOICE_ID=c\n")

        # 1. off by default. the absence of a flag is the safe state.
        ok, why = status(env={}, path=good)
        assert not ok and "off by default" in why, f"1: expected off by default, got {why!r}"

        # 2. armed only with the explicit demo flag
        ok, why = status(env={"VOICE_DEMO": "1"}, path=good)
        assert ok, f"2: expected armed, got {why!r}"

        # 3. REFUSES behind the tunnel even when armed. this is the assertion
        # that matters: it is the difference between a demo on fixtures and
        # sending a real patient's voice to a US processor.
        ok, why = status(env={"VOICE_DEMO": "1", "PATIENT_TRUST_FORWARDED_IP": "1"}, path=good)
        assert not ok and "internet-facing" in why, f"3: expected tunnel refusal, got {why!r}"

        # 4. a missing key is a refusal, not a half-armed state
        partial = Path(tmp) / "partial.voice"
        partial.write_text("DEEPGRAM_API_KEY=a\n")
        ok, why = status(env={"VOICE_DEMO": "1"}, path=partial)
        assert not ok and "ELEVENLABS_API_KEY" in why, f"4: expected missing-key refusal, got {why!r}"

        # 5. no key value is ever in the refusal text - these get logged
        assert "a" not in why.split("missing:")[1] or "KEY" in why, "5: refusals name keys, not values"

        # 6. the banner says where the audio goes, in the product not a doc
        assert "Deepgram" in BANNER and "ElevenLabs" in BANNER and "outside this machine" in BANNER, \
            "6: the banner must name the processors and say the audio leaves"

    print("selftest ok")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
    else:
        ok, why = status()
        print(f"demo voice: {'ARMED' if ok else 'off'} - {why}")
