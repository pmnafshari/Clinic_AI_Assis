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

# Third-party notices - documents and OCR (P15)

| Component | Version / revision | Licence | Notes |
|---|---|---|---|
| pypdf | 6.19.0 | BSD-3-Clause | PDF text only; runs in the sandboxed worker; never renders or executes anything |
| Tesseract OCR | 5.5.3 (Homebrew) | Apache-2.0 | system binary, called by the sandboxed worker; not bundled |
| tessdata_fast `ita`, `eng` | `tesseract-ocr/tessdata_fast` @ `87416418657359cb625c412a48b6e1d6d41c29bd`; sha256 `b8f89e1e785118da…` (ita), `7d4322bd2a774972…` (eng) | Apache-2.0 | stored in `models/ocr/tessdata/` (git-ignored) |
| all-MiniLM-L6-v2 (via chromadb) | already cached by the project | Apache-2.0 | local text embedder; refused, never downloaded, if missing |

Not used: poppler/`pdftotext` (GPL, installed on the machine but not called) - kept out for the same
reason text-to-speech is blocked: the project's licence and distribution plan are undecided.

# Third-party notices - Jarvis wake phrase and listening (J01)

| Component | Version / file sha256 | Licence | Notes |
|---|---|---|---|
| sounddevice (bundles PortAudio V19.7) | 0.5.6 | MIT; PortAudio licence (MIT-style) | microphone capture at 16 kHz; nothing is written or sent |
| rumps | 0.4.0 | BSD | menu-bar indicator |
| PyObjC (core, Cocoa) | 12.2.2 | MIT | required by rumps |
| openWakeWord feature models `melspectrogram.onnx`, `embedding_model.onnx` (release v0.5.1) | `ba2b0e0f8b7b875369a2…`, `70d164290c1d095d1d4e…` | melspectrogram: an ONNX export of a fixed Torch function (no training data); embedding: openWakeWord's reimplementation of Google's `speech_embedding` model (published by Google under **Apache 2.0**) | stored in `models/jarvis/features/` (git-ignored). The release assets carry no per-file licence; provenance rests on the openWakeWord README (code Apache-2.0) and Google's model. The streaming design follows openWakeWord (Apache-2.0, © David Scripka) and is reimplemented in `jarvis/features.py`. |
| openWakeWord pre-trained wake models | — | CC BY-NC-SA 4.0 (non-commercial) | **not downloaded, installed or shipped**; the `openwakeword` package is not installed |
| Custom "Hey Jarvis" model `models/jarvis/wake/hey_jarvis.npz` | recorded in the J01 evidence | this project's own | trained locally from the sources below; nothing left the Mac |
| Piper voice `en_US-libritts-high` (used only to generate training audio, never shipped to speak) | `9127a559e11603f10b36…` | voices repository MIT; model card: trained from scratch on LibriTTS train-clean-360 | **Attribution:** LibriTTS, Zen et al. 2019, OpenSLR 60, **CC BY 4.0**. Phonemes written by hand; espeak-ng (GPL-3.0) not used. `models/jarvis/generator/` (git-ignored) |
| LibriSpeech dev-clean, test-clean, test-other | OpenSLR 12 | **CC BY 4.0** | **Attribution:** LibriSpeech ASR corpus, Panayotov, Chen, Povey, Khudanpur, 2015. Training negatives (dev-clean) and the false-wake evaluation (test sets). Kept outside the repository |

Rejected: `en_US-libritts_r-medium` and the VCTK/HFC voices (fine-tuned from the lessac voice, whose dataset licence is
research-only, or CC BY-NC-SA). Text-to-speech for answers is still not installed (POL-13).
