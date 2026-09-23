# Third-party notices - local speech-to-text

A technical licence inventory, not legal advice. Everything below runs on this machine only.

| Component | Version / revision | Code licence | Notes |
|---|---|---|---|
| faster-whisper (SYSTRAN) | 1.2.1 (wheel sha256 `79a66ad50688c0b7…`) | MIT, © 2023 SYSTRAN | Python library, imported in-process |
| CTranslate2 (OpenNMT) | 4.8.2 (macOS arm64 cp312 wheel sha256 `fedda669421a57f8…`) | MIT | inference engine, CPU, int8 |
| onnxruntime (Microsoft) | 1.27.0 (sha256 `a14c2ce45312def8…`) | MIT | pulled in by faster-whisper for its voice-activity filter, which this project does not enable |
| tokenizers (Hugging Face) | 0.23.1 | Apache-2.0 | |
| huggingface_hub, hf-xet (Hugging Face) | 1.22.0, 1.5.1 | Apache-2.0 | used once, at setup, to fetch the model; forced offline at runtime |
| PyAV | 18.1.0 | BSD-3-Clause | required by faster-whisper; **not used to decode** here (WAV is decoded with the standard library). Its wheel bundles FFmpeg, which reports itself as **LGPL-3.0-or-later** but is built with libx264/libx265 (GPL). Open question for a licence review before any distribution. |
| Whisper small weights (OpenAI), converted to CTranslate2 by SYSTRAN | `Systran/faster-whisper-small` @ `536b0662742c02347bc0e980a01041f333bce120`, `model.bin` sha256 `3e305921506d8872…` | MIT (Systran model card); OpenAI's Whisper repository states code **and weights** MIT, © 2022 OpenAI. OpenAI's own Hugging Face card for `openai/whisper-small` says Apache-2.0 - recorded as a discrepancy; both are permissive. | converted from `openai/whisper-small` with `ct2-transformers-converter --quantization float16` (per the Systran card) |

MIT notices: "Permission is hereby granted, free of charge, to any person obtaining a copy of this
software ... THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND" - full texts at
https://github.com/SYSTRAN/faster-whisper/blob/master/LICENSE,
https://github.com/openai/whisper/blob/main/LICENSE,
https://github.com/OpenNMT/CTranslate2/blob/master/LICENSE.

Text-to-speech: none installed. See the P14 evidence for why it is still blocked.
