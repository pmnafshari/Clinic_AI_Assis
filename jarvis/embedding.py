"""openWakeWord's shared speech embedding (a reimplementation of Google's speech_embedding, Apache 2.0), run as a stream.

The network shrinks time only through three 2x max-pools and never pads time, and its 76-frame window moves 8 frames per
80 ms chunk, so every window is a slice of one continuous computation: each layer keeps the few rows it still needs and
computes only the new ones. Same weights (read by jarvis/onnx_layers.py from the pinned file) and the same function as
embedding_model.onnx run window by window, for about a third of the work per chunk.
"""
import numpy as np

WINDOW_FRAMES = 76      # mel frames per embedding (775 ms)
HOP = 8                 # mel frames per 80 ms chunk


def conv(x, layer):
    """[time, freq, in] -> [time - kt + 1, freq, out]: unpadded in time, padded in frequency."""
    _, wm, b, kt, kf, pad, act = layer
    if pad:
        x = np.pad(x, ((0, 0), (pad, pad), (0, 0)))
    t, f = len(x) - kt + 1, x.shape[1] - kf + 1
    cols = np.concatenate([x[a:a + t, c:c + f] for a in range(kt) for c in range(kf)], axis=2)
    y = (cols.reshape(t * f, -1) @ wm + b).reshape(t, f, -1)
    if act:
        y = np.maximum(np.maximum(y, 0.2 * y), -0.4)     # LeakyReLU(0.2), then the network's floor of -0.4
    return y


def pool(x, kt):
    """Max over kt time rows and 2 frequency bins."""
    t, f, c = x.shape
    return x.reshape(t // kt, kt, f // 2, 2, c).max(axis=(1, 3))


class Stream:
    def __init__(self, layers):
        """layers: ("conv", w [out, in, time, freq], bias, frequency padding, activation) or ("pool", time)."""
        self.layers = []
        for layer in layers:
            if layer[0] == "pool":
                self.layers.append(layer)
                continue
            _, w, b, pad, act = layer
            out, cin, kt, kf = w.shape
            wm = np.ascontiguousarray(w.transpose(2, 3, 1, 0).reshape(kt * kf * cin, out), np.float32)
            self.layers.append(("conv", wm, np.asarray(b, np.float32), kt, kf, pad, act))
        self.kept = [None] * len(self.layers)

    def reset(self, frames):
        """Start a new stream at one full window of mel frames -> that window's embedding."""
        if len(frames) != WINDOW_FRAMES:
            raise ValueError(f"a stream starts with {WINDOW_FRAMES} frames, not {len(frames)}")
        self.kept = [None] * len(self.layers)
        return self._run(frames)[0]

    def push(self, frames):
        """HOP new mel frames per chunk -> one embedding per chunk."""
        if len(frames) % HOP:
            raise ValueError(f"frames come {HOP} per chunk, not {len(frames)}")
        return self._run(frames)

    def _run(self, frames):
        x = np.asarray(frames, np.float32)[:, :, None]
        for i, layer in enumerate(self.layers):
            if layer[0] == "pool":
                x = pool(x, layer[1])
                continue
            if self.kept[i] is not None:
                x = np.concatenate([self.kept[i], x])
            self.kept[i] = x[len(x) - layer[3] + 1:]       # the rows the next chunk's outputs still need
            x = conv(x, layer)
        return x.reshape(len(x), -1)
