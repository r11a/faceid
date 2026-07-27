"""Sample a recorded event clip and return diverse, quality-ranked face observations."""
import os
import tempfile
from dataclasses import dataclass

import cv2
import numpy as np

from .quality import FaceQuality, measure_face_quality


@dataclass
class FaceSample:
    frame: np.ndarray
    face: object
    quality: FaceQuality
    frame_index: int
    track: int


class ClipAnalyzer:
    def __init__(
        self, engine, frigate, *, max_frames: int = 24, max_samples: int = 8,
        min_face_px: int = 48, min_quality: float = 0.35,
        track_similarity: float = 0.55, diversity_similarity: float = 0.995,
    ):
        self.engine = engine
        self.frigate = frigate
        self.max_frames = max(4, int(max_frames))
        self.max_samples = max(2, int(max_samples))
        self.min_face_px = int(min_face_px)
        self.min_quality = float(min_quality)
        self.track_similarity = float(track_similarity)
        self.diversity_similarity = float(diversity_similarity)

    def analyze(self, event_id: str, reference_embedding=None) -> list[FaceSample]:
        fd, path = tempfile.mkstemp(suffix=".mp4", prefix="faceid-analyze-")
        os.close(fd)
        try:
            if not self.frigate.download_clip(event_id, path):
                return []
            return self._analyze_file(path, reference_embedding)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    def _analyze_file(self, path: str, reference_embedding=None) -> list[FaceSample]:
        cap = cv2.VideoCapture(path)
        try:
            total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            if total <= 0:
                return []
            indices = np.linspace(
                0, total - 1, min(self.max_frames, total), dtype=int
            )
            tracks: list[dict] = []
            for frame_index in indices:
                cap.set(cv2.CAP_PROP_POS_FRAMES, int(frame_index))
                ok, frame = cap.read()
                if not ok or frame is None:
                    continue
                for face in self.engine.faces(frame):
                    quality = measure_face_quality(
                        frame, face, min_face_px=self.min_face_px,
                        min_quality=self.min_quality,
                    )
                    if not quality.usable:
                        continue
                    emb = face.normed_embedding
                    track = self._track_for(tracks, emb)
                    tracks[track]["embeddings"].append(emb)
                    tracks[track]["samples"].append(
                        FaceSample(frame, face, quality, int(frame_index), track)
                    )

            if not tracks:
                return []
            selected_track = self._select_track(tracks, reference_embedding)
            samples = sorted(
                tracks[selected_track]["samples"],
                key=lambda sample: sample.quality.score,
                reverse=True,
            )
            diverse = []
            for sample in samples:
                emb = sample.face.normed_embedding
                if any(
                    float(emb @ kept.face.normed_embedding) >= self.diversity_similarity
                    for kept in diverse
                ):
                    continue
                diverse.append(sample)
                if len(diverse) >= self.max_samples:
                    break
            return sorted(diverse, key=lambda sample: sample.frame_index)
        finally:
            cap.release()

    def _track_for(self, tracks: list[dict], embedding) -> int:
        best_idx, best_score = None, -1.0
        for idx, track in enumerate(tracks):
            center = np.mean(track["embeddings"], axis=0)
            center /= max(float(np.linalg.norm(center)), 1e-9)
            score = float(center @ embedding)
            if score > best_score:
                best_idx, best_score = idx, score
        if best_idx is None or best_score < self.track_similarity:
            tracks.append({"embeddings": [], "samples": []})
            return len(tracks) - 1
        return best_idx

    @staticmethod
    def _select_track(tracks: list[dict], reference_embedding) -> int:
        ranked = []
        for idx, track in enumerate(tracks):
            embeddings = track["embeddings"]
            quality = sum(s.quality.score for s in track["samples"])
            if reference_embedding is None:
                identity = 0.0
            else:
                identity = max(float(emb @ reference_embedding) for emb in embeddings)
            ranked.append((identity, len(embeddings), quality, idx))
        return max(ranked)[-1]
