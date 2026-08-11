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
        media_store=None, max_faces_per_frame: int = 4, frame_distributor=None,
    ):
        self.engine = engine
        self.frigate = frigate
        self.max_frames = max(4, int(max_frames))
        self.max_samples = max(2, int(max_samples))
        self.min_face_px = int(min_face_px)
        self.min_quality = float(min_quality)
        self.track_similarity = float(track_similarity)
        self.diversity_similarity = float(diversity_similarity)
        self.media_store = media_store
        self.max_faces_per_frame = max(1, min(int(max_faces_per_frame), 12))
        self.frame_distributor = frame_distributor

    def analyze(self, event_id: str, reference_embedding=None, *, min_face_px=None, roi=None) -> list[FaceSample]:
        effective_min_face_px = int(min_face_px or self.min_face_px)
        if self.frame_distributor is not None:
            return self._analyze_frames(
                self.frame_distributor.frames(event_id, limit=self.max_frames),
                reference_embedding, effective_min_face_px, roi,
            )
        if self.media_store is not None:
            path = self.media_store.clip_path(event_id)
            return self._analyze_file(str(path), reference_embedding, effective_min_face_px, roi) if path else []
        fd, path = tempfile.mkstemp(suffix=".mp4", prefix="faceid-analyze-")
        os.close(fd)
        try:
            if not self.frigate.download_clip(event_id, path):
                return []
            return self._analyze_file(path, reference_embedding, effective_min_face_px, roi)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    def _analyze_file(self, path: str, reference_embedding=None, min_face_px=None, roi=None) -> list[FaceSample]:
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
                measured = []
                faces = self._within_roi(self.engine.faces(frame), frame, roi)
                for face in faces:
                    quality = measure_face_quality(
                        frame, face, min_face_px=min_face_px or self.min_face_px,
                        min_quality=self.min_quality,
                    )
                    if not quality.usable:
                        continue
                    measured.append((quality.score, quality, face))
                # A pathological detector result must not retain hundreds of copies
                # of a 4K frame until the event finishes.
                measured.sort(key=lambda item: item[0], reverse=True)
                encoded = None
                for _, quality, face in measured[:self.max_faces_per_frame]:
                    if encoded is None:
                        ok, buffer = cv2.imencode(
                            ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 88]
                        )
                        if not ok:
                            break
                        encoded = buffer.tobytes()
                    emb = face.normed_embedding
                    track = self._track_for(tracks, emb)
                    tracks[track]["embeddings"].append(emb)
                    tracks[track]["samples"].append(
                        FaceSample(encoded, face, quality, int(frame_index), track)
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
            for sample in diverse:
                sample.frame = cv2.imdecode(
                    np.frombuffer(sample.frame, np.uint8), cv2.IMREAD_COLOR
                )
            return sorted(
                (sample for sample in diverse if sample.frame is not None),
                key=lambda sample: sample.frame_index,
            )
        finally:
            cap.release()

    def _analyze_frames(self, frames, reference_embedding=None, min_face_px=None, roi=None) -> list[FaceSample]:
        tracks: list[dict] = []
        for frame_index, frame in frames:
            measured = []
            faces = self._within_roi(self.engine.faces(frame), frame, roi)
            for face in faces:
                quality = measure_face_quality(frame, face, min_face_px=min_face_px or self.min_face_px,
                                               min_quality=self.min_quality)
                if quality.usable:
                    measured.append((quality.score, quality, face))
            measured.sort(key=lambda item: item[0], reverse=True)
            for _, quality, face in measured[:self.max_faces_per_frame]:
                track = self._track_for(tracks, face.normed_embedding)
                tracks[track]["embeddings"].append(face.normed_embedding)
                tracks[track]["samples"].append(FaceSample(frame.copy(), face, quality, int(frame_index), track))
        if not tracks:
            return []
        selected = self._select_track(tracks, reference_embedding)
        samples = sorted(tracks[selected]["samples"], key=lambda row: row.quality.score, reverse=True)
        diverse = []
        for sample in samples:
            if any(float(sample.face.normed_embedding @ kept.face.normed_embedding) >= self.diversity_similarity
                   for kept in diverse):
                continue
            diverse.append(sample)
            if len(diverse) >= self.max_samples:
                break
        return sorted(diverse, key=lambda row: row.frame_index)

    @staticmethod
    def _within_roi(faces, frame, roi):
        if not isinstance(roi, list) or len(roi) != 4:
            return faces
        height, width = frame.shape[:2]
        left, top, right, bottom = roi
        return [face for face in faces if (
            left <= float(face.bbox[0] + face.bbox[2]) / 2 / max(width, 1) <= right
            and top <= float(face.bbox[1] + face.bbox[3]) / 2 / max(height, 1) <= bottom
        )]

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
