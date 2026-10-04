"""Audio features for the wake phrase, 16 kHz mono in 80 ms chunks.

The streaming design follows openWakeWord (Apache-2.0), reimplemented here so its package - which can fetch its
non-commercial wake models - is never installed. It runs openWakeWord's two shared feature models, sha256-pinned in
models/jarvis/SHA256SUMS: melspectrogram.onnx (an ONNX form of a fixed Torch melspectrogram, no training data) and
embedding_model.onnx (a reimplementation of Google's speech_embedding, Apache 2.0).
"""
from pathlib import Path

import numpy as np

ROOT = Path("models/jarvis/features")
RATE = 16000
CHUNK = 1280            # 80 ms: one embedding per chunk
CONTEXT = 480           # three 10 ms hops the melspectrogram needs from the chunk before
MEL_WINDOW = 76         # mel frames per embedding (775 ms)
MEL_PER_CHUNK = 8
MEL_KEEP = MEL_WINDOW + MEL_PER_CHUNK * 15   # enough to rebuild every embedding of a 16-chunk window


class Features:
    def __init__(self, root=ROOT, sessions=None):
        if sessions:
            self.mel, self.emb = sessions
            return self.reset()
        import onnxruntime as ort
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1      # an always-on listener must stay small
        opts.inter_op_num_threads = 1
        cpu = ["CPUExecutionProvider"]
        self.mel = ort.InferenceSession(str(Path(root) / "melspectrogram.onnx"), opts, providers=cpu)
        self.emb = ort.InferenceSession(str(Path(root) / "embedding_model.onnx"), opts, providers=cpu)
        self.reset()

    def reset(self):
        self.tail = np.zeros(CONTEXT, np.float32)
        self.mels = np.ones((MEL_KEEP, 32), np.float32)

    def add_mel(self, chunk):
        """The cheap part, always run: one 80 ms chunk of int16 samples -> 8 mel frames."""
        x = np.concatenate([self.tail, np.asarray(chunk, np.float32)])
        self.tail = x[-CONTEXT:]
        spec = self.mel.run(None, {"input": x[None, :]})[0].reshape(-1, 32) / 10 + 2
        self.mels = np.vstack([self.mels, spec])[-MEL_KEEP:]

    def embedding(self, back=0):
        """The costly part: the 96-value embedding of the chunk `back` chunks ago (up to 15)."""
        end = len(self.mels) - MEL_PER_CHUNK * back
        return self.emb.run(None, {"input_1": self.mels[None, end - MEL_WINDOW:end, :, None]})[0].reshape(96)

    def feed(self, chunk):
        self.add_mel(chunk)
        return self.embedding()
