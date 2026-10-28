"""Bounded, camera-local face continuity; recognition policy lives in the caller."""

from dataclasses import dataclass
import math
import uuid

import numpy as np


@dataclass
class _Track:
    bbox: np.ndarray
    embedding: np.ndarray
    last_seen: float
    missing: bool = False


class FaceTracker:
    """Assign conservative encounter IDs. Calls for one camera must be serialized.

    A match requires cosine >= .65, a .08 margin over alternative matches
    on both sides, and plausible box motion. Uncertain faces start fresh tracks
    (or are omitted at capacity), so confirmation cannot cross an ambiguous match.
    """

    def __init__(self, absence_seconds=2.0, max_tracks=32):
        self.absence_seconds = absence_seconds
        self.max_tracks = max(1, int(max_tracks))
        self._tracks = {}
        self._last_now = None

    def _time(self, now):
        now = float(now)
        if not math.isfinite(now):
            raise ValueError('Tracking time must be finite')
        if self._last_now is not None:
            now = max(now, self._last_now)
        self._last_now = now
        return now

    def touch(self, track_ids, now):
        """Refresh only named existing tracks for a cached visible frame.

        This creates no observations or assignments. The next update owns expiry
        reporting; callers must only supply IDs visible in their cached frame.
        """
        now = self._time(now)
        for track_id in track_ids:
            track = self._tracks.get(track_id)
            if track is not None:
                track.last_seen = now
                track.missing = False

    @staticmethod
    def _observation(face):
        try:
            bbox = np.asarray(face.bbox, dtype=float)
            embedding = np.asarray(face.normed_embedding, dtype=float)
            if (bbox.shape != (4,) or not np.isfinite(bbox).all()
                    or np.any(bbox[2:] <= bbox[:2])
                    or embedding.ndim != 1 or not embedding.size
                    or not np.isfinite(embedding).all()):
                return None
            norm = float(np.linalg.norm(embedding))
            if not math.isfinite(norm) or norm <= 1e-12:
                return None
            return bbox.copy(), embedding / norm
        except (AttributeError, TypeError, ValueError):
            return None

    @staticmethod
    def _plausible_motion(previous, current):
        old_size = previous[2:] - previous[:2]
        new_size = current[2:] - current[:2]
        ratio = new_size / old_size
        if np.any(ratio < 1 / 3) or np.any(ratio > 3):
            return False
        displacement = np.linalg.norm((current[:2] + current[2:]
                                       - previous[:2] - previous[2:]) / 2)
        return displacement <= 2 * min(np.linalg.norm(old_size), np.linalg.norm(new_size))

    @staticmethod
    def _unique_best(scores):
        if not len(scores):
            return None
        best = int(np.argmax(scores))
        if scores[best] < .65:
            return None
        alternatives = np.delete(scores, best)
        if len(alternatives) and scores[best] - np.max(alternatives) < .08:
            return None
        return best

    def update(self, faces, now):
        """Return ([(track_id, face), ...], expired_ids) in input face order.

        Faces with invalid boxes/embeddings and unmatched faces at capacity are
        omitted. At most max_tracks prototypes are retained, with no history.
        Expiry requires observed absence: slow sampling alone cannot end a track
        that still matches the next decoded frame.
        """
        now = self._time(now)
        absence = max(0.0, float(self.absence_seconds))
        expired = [key for key, track in self._tracks.items()
                   if track.missing and now - track.last_seen >= absence]
        for key in expired:
            del self._tracks[key]

        observations = []
        for face in faces:
            observation = self._observation(face)
            if observation is not None:
                observations.append((face, *observation))

        track_ids = list(self._tracks)
        scores = np.full((len(observations), len(track_ids)), -np.inf)
        for row, (_, bbox, embedding) in enumerate(observations):
            for col, track_id in enumerate(track_ids):
                track = self._tracks[track_id]
                if (embedding.shape == track.embedding.shape
                        and self._plausible_motion(track.bbox, bbox)):
                    scores[row, col] = float(np.dot(embedding, track.embedding))

        # Evaluate both directions before any mutation; a greedy match could
        # otherwise hide ambiguity by consuming a competing track first.
        best_tracks = [self._unique_best(row) for row in scores]
        best_faces = [self._unique_best(scores[:, col]) for col in range(len(track_ids))]
        matched = {track_ids[col] for row, col in enumerate(best_tracks)
                   if col is not None and best_faces[col] == row}
        # The decoded frame is evidence of absence for every unmatched track.
        # Free expired capacity before assigning newcomers from this same frame.
        for track_id in track_ids:
            if track_id not in matched:
                track = self._tracks[track_id]
                track.missing = True
                if now - track.last_seen >= absence:
                    expired.append(track_id)
                    del self._tracks[track_id]
        assignments = []
        for row, (face, bbox, embedding) in enumerate(observations):
            col = best_tracks[row]
            if col is not None and best_faces[col] == row:
                track_id = track_ids[col]
                track = self._tracks[track_id]
                # Keep the original embedding as an anchor, preventing a chain
                # of marginal observations from drifting into another person.
                track.bbox = bbox
                track.last_seen = now
                track.missing = False
            elif len(self._tracks) < self.max_tracks:
                track_id = 'faceid-' + uuid.uuid4().hex
                self._tracks[track_id] = _Track(bbox, embedding, now)
            else:
                continue
            assignments.append((track_id, face))
        return assignments, expired
