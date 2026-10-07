"""Audio features for the wake phrase, 16 kHz mono in 80 ms chunks.

The streaming design follows openWakeWord (Apache-2.0), reimplemented here so its package - which can fetch its
non-commercial wake models - is never installed. It uses openWakeWord's two shared feature models, sha256-pinned in
models/jarvis/SHA256SUMS: melspectrogram.onnx (an ONNX form of a fixed Torch melspectrogram, no training data), run by
onnxruntime, and embedding_model.onnx (a reimplementation of Google's speech_embedding, Apache 2.0), whose weights
jarvis/onnx_layers.py reads for jarvis/embedding.py to run as a stream.
"""
from pathlib import Path

import numpy as np

from jarvis import onnx_layers
from jarvis.embedding import WINDOW_FRAMES, Stream

ROOT = Path("models/jarvis/features")
RATE = 16000
CHUNK = 1280            # 80 ms: one embedding per chunk
CONTEXT = 480           # three 10 ms hops the melspectrogram needs from the chunk before


class Features:
    def __init__(self, root=ROOT, mel=None, stream=None):
        if mel is None:
            import onnxruntime as ort
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = 1      # an always-on listener must stay small
            opts.inter_op_num_threads = 1
            mel = ort.InferenceSession(str(Path(root) / "melspectrogram.onnx"), opts, providers=["CPUExecutionProvider"])
        if stream is None:
            stream = Stream(onnx_layers.layers(Path(root) / "embedding_model.onnx"))
        self.mel, self.stream = mel, stream
        self.reset()

    def reset(self):
        self.tail = np.zeros(CONTEXT, np.float32)
        self.stream.reset(np.ones((WINDOW_FRAMES, 32), np.float32))    # a blank start, as openWakeWord's

    def add(self, samples):
        """Whole 80 ms chunks of int16 samples -> one 96-value embedding per chunk."""
        frames = []
        for chunk in np.asarray(samples, np.float32).reshape(-1, CHUNK):
            # one melspectrogram call per chunk, as openWakeWord does: the model floors each call at its own loudest
            # frame - 80 dB, so a call over two chunks would change the quieter one
            x = np.concatenate([self.tail, chunk])
            self.tail = x[-CONTEXT:]
            frames.append(self.mel.run(None, {"input": x[None, :]})[0].reshape(-1, 32) / 10 + 2)
        return self.stream.push(np.concatenate(frames))
