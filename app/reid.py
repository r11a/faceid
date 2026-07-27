"""Short-lived clothing appearance hints, never promoted to a face identity verdict."""
import threading
import time
from collections import deque

import cv2
import numpy as np


class AppearanceReID:
    def __init__(self, *, ttl_seconds: float = 180, threshold: float = 0.90,
                 camera_graph=None):
        self.ttl_seconds = float(ttl_seconds)
        self.threshold = float(threshold)
        self.camera_graph = {
            camera: set(neighbors)
            for camera, neighbors in (camera_graph or {}).items()
        }
        self._items = deque(maxlen=200)
        self._lock = threading.Lock()

    @staticmethod
    def embedding(bgr):
        if bgr is None or bgr.size == 0:
            return None
        h = bgr.shape[0]
        clothing = bgr[int(h * 0.35):]
        if clothing.size == 0:
            return None
        hsv = cv2.cvtColor(clothing, cv2.COLOR_BGR2HSV)
        hist = cv2.calcHist([hsv], [0, 1], None, [24, 16], [0, 180, 0, 256])
        vector = hist.flatten().astype(np.float32)
        norm = float(np.linalg.norm(vector))
        return vector / norm if norm > 0 else None

    def seed(self, person: str, camera: str, bgr, ts: float | None = None):
        vector = self.embedding(bgr)
        if vector is None:
            return
        with self._lock:
            self._items.append({
                "person": person, "camera": camera, "ts": ts or time.time(),
                "embedding": vector,
            })

    def match(self, camera: str, bgr, ts: float | None = None):
        vector = self.embedding(bgr)
        if vector is None:
            return None, 0.0
        now = ts or time.time()
        best = (None, 0.0)
        with self._lock:
            while self._items and now - self._items[0]["ts"] > self.ttl_seconds:
                self._items.popleft()
            for item in self._items:
                if self.camera_graph:
                    allowed = self.camera_graph.get(item["camera"], set())
                    if camera != item["camera"] and camera not in allowed:
                        continue
                score = float(vector @ item["embedding"])
                if score > best[1]:
                    best = (item["person"], score)
        return best if best[1] >= self.threshold else (None, best[1])
