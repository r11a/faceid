"""Turn noisy detector observations into useful human-facing visits."""
from __future__ import annotations

import threading
import time


class RecognitionSessionTracker:
    """Classify a recognition as an arrival, camera move, or quiet update.

    Every observation refreshes the session. A continuously visible person therefore
    produces one arrival, while a real absence followed by a return starts a new visit.
    """

    def __init__(self, gap_seconds: float = 300):
        self.gap_seconds = max(30.0, float(gap_seconds))
        self._sessions: dict[str, dict] = {}
        self._lock = threading.Lock()

    def classify(self, person: str, camera: str, ts: float | None = None) -> str:
        observed_ts = float(ts or time.time())
        key = str(person).strip().casefold()
        with self._lock:
            previous = self._sessions.get(key)
            if previous is None or observed_ts - previous["ts"] > self.gap_seconds:
                occurrence = "arrival"
            elif previous["camera"] != camera:
                occurrence = "camera_transition"
            else:
                occurrence = "presence_update"
            self._sessions[key] = {"camera": camera, "ts": observed_ts}
            cutoff = observed_ts - self.gap_seconds * 4
            self._sessions = {
                session_key: session
                for session_key, session in self._sessions.items()
                if session["ts"] >= cutoff
            }
            return occurrence
