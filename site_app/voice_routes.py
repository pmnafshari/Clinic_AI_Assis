"""Voice transport for the public clinic assistant.

Microphone -> Deepgram STT -> the clinic answer router -> Deepgram Aura TTS
-> audio back to the browser. Every hop is real; nothing here simulates a
step it cannot perform.

Two rules this file exists to keep:

  * THE KEYS NEVER REACH THE BROWSER. Audio is posted here, this server
    calls the vendors, and only text and audio come back. The page has no
    credential of any kind.
  * NO RECORDING IS KEPT. The uploaded bytes live in the request and are
    never written to disk, never logged, and never put in a database. This
    app has no database to put them in.

The fence in voice_config.py still applies: everything here is off unless
VOICE_DEMO=1, and /assistant/voice/status tells the page so it can fall back
to typing rather than offering a control that cannot work.
"""

import base64

from flask import jsonify, request

import voice
import voice_config

from . import clinic_answers

# a spoken turn is short; this bounds what a stranger can push through a
# billed API on an unauthenticated page
MAX_UPLOAD = 2 * 1024 * 1024


def register(app, clinic):
    @app.route("/assistant/voice/status")
    def voice_status():
        ok, why = voice_config.status()
        # the reason is a fence message, never a vendor error or a key
        return jsonify({"available": ok, "reason": why if not ok else ""})

    @app.route("/assistant/voice/greeting")
    def voice_greeting():
        ok, why = voice_config.status()
        if not ok:
            return jsonify({"error": why}), 503
        try:
            audio = voice.speak_aura(clinic["assistant"]["greeting"])
        except voice.VoiceUnavailable as e:
            return jsonify({"error": str(e)}), 502
        return jsonify({"text": clinic["assistant"]["greeting"],
                        "audio": base64.b64encode(audio).decode()})

    @app.route("/assistant/voice", methods=["POST"])
    def voice_turn():
        ok, why = voice_config.status()
        if not ok:
            return jsonify({"error": why}), 503

        blob = request.files.get("audio")
        if blob is None:
            return jsonify({"error": "no audio in the request"}), 400
        data = blob.read(MAX_UPLOAD + 1)
        if len(data) > MAX_UPLOAD:
            return jsonify({"error": "recording too long"}), 413
        if not data:
            return jsonify({"error": "empty recording"}), 400

        try:
            transcript = voice.transcribe(
                data, content_type=blob.mimetype or "audio/webm", language="en")
        except voice.VoiceUnavailable as e:
            return jsonify({"error": str(e)}), 502
        # the uploaded bytes go out of scope here. nothing is stored.
        del data

        if not transcript:
            # silence is a real outcome, not an error, and must not be
            # answered as though something was said
            return jsonify({"transcript": "", "state": "silence",
                            "text": "I did not catch that. Try again, or type your question."})

        state, reply = clinic_answers.answer(transcript, clinic)
        try:
            audio = voice.speak_aura(reply)
        except voice.VoiceUnavailable:
            # the answer is real even when speech fails - return it as text
            return jsonify({"transcript": transcript, "state": state, "text": reply, "audio": None})
        return jsonify({"transcript": transcript, "state": state, "text": reply,
                        "audio": base64.b64encode(audio).decode()})
