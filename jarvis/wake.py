"""The custom wake phrase: a small classifier over the last 1.28 s of audio features, and the detector around it.

The classifier is this project's own (trained locally on licensed and synthetic audio; provenance in
.planning/plans/JARVIS.md §9). It returns a score; the detector wants two chunks in a row over the threshold and then
rests for 2 s, so one utterance wakes Jarvis once. Features are computed for two chunks at a time (half the cost of
waking up every 80 ms), then each chunk is judged on its own, so a wake is reported at most one chunk later. While the
whole window is at the room's noise floor no phrase can be in it, so the classifier is skipped.
"""
import json
from collections import deque
from pathlib import Path

import numpy as np

MODEL = Path("models/jarvis/wake/hey_jarvis.npz")
WINDOW = 16             # embeddings the classifier sees (16 x 80 ms)
HITS = 2                # consecutive chunks over the threshold
REST_CHUNKS = 25        # 2 s after a wake
FLOOR_CHUNKS = 125      # the room's noise floor: the quietest tenth of the last 10 s
QUIET_FACTOR = 1.5
BATCH = 2               # chunks per feature computation


class WakeModel:
    def __init__(self, path=MODEL):
        data = np.load(path)
        self.mean, self.std = data["mean"], data["std"]
        self.w1, self.b1, self.w2, self.b2 = data["w1"], data["b1"], data["w2"], float(data["b2"])
        self.threshold = float(data["threshold"])
        self.info = json.loads(str(data["info"])) if "info" in data else {}

    def score(self, window):
        x = (np.asarray(window, np.float32).reshape(-1) - self.mean) / self.std
        h = np.maximum(x @ self.w1 + self.b1, 0)
        return float(1 / (1 + np.exp(-np.clip(h @ self.w2 + self.b2, -30, 30))))


class Detector:
    def __init__(self, model, features):
        self.model, self.features = model, features
        self.reset()

    def reset(self):
        self.pending = []
        self.window = deque(maxlen=WINDOW)
        self.levels = deque(maxlen=FLOOR_CHUNKS)
        self.quiet_run = 0
        self.skipped = False
        self.hits = 0
        self.rest = 0
        self.last_score = 0.0

    def feed(self, chunk):
        """One 80 ms chunk -> True when the wake phrase has just been heard."""
        self.pending.append(chunk)
        if len(self.pending) < BATCH:
            return False
        chunks, self.pending = self.pending, []
        woke = False
        for c, e in zip(chunks, self.features.add(np.concatenate(chunks))):
            woke = self._judge(c, e) or woke
        return woke

    def _judge(self, chunk, embedding):
        level = float(np.sqrt(np.mean(np.asarray(chunk, np.float32) ** 2)))
        self.levels.append(level)
        floor = float(np.percentile(self.levels, 10))
        quiet = len(self.levels) >= WINDOW and level <= floor * QUIET_FACTOR
        self.quiet_run = self.quiet_run + 1 if quiet else 0
        self.skipped = self.quiet_run >= WINDOW
        self.window.append(embedding)
        if self.skipped:
            self.hits, self.last_score = 0, 0.0
            self.rest = max(0, self.rest - 1)
            return False
        if len(self.window) < WINDOW:
            return False
        self.last_score = self.model.score(self.window)
        if self.rest:
            self.rest -= 1
            return False
        self.hits = self.hits + 1 if self.last_score >= self.model.threshold else 0
        if self.hits >= HITS:
            self.hits, self.rest = 0, REST_CHUNKS
            return True
        return False
