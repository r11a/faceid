"""Objective face-quality measurements used before recognition evidence is counted."""
from dataclasses import asdict, dataclass

import cv2
import numpy as np


@dataclass
class FaceQuality:
    score: float
    face_px: int
    detection: float
    sharpness: float
    illumination: float
    contrast: float
    frontal: float
    usable: bool

    def to_dict(self):
        return asdict(self)


def measure_face_quality(
    bgr: np.ndarray,
    face,
    *,
    min_face_px: int = 48,
    min_quality: float = 0.35,
) -> FaceQuality:
    x1, y1, x2, y2 = [int(v) for v in face.bbox]
    h, w = bgr.shape[:2]
    x1, y1, x2, y2 = max(0, x1), max(0, y1), min(w, x2), min(h, y2)
    crop = bgr[y1:y2, x1:x2]
    face_px = min(max(0, x2 - x1), max(0, y2 - y1))
    if crop.size == 0:
        return FaceQuality(0.0, face_px, 0.0, 0.0, 0.0, 0.0, 0.0, False)

    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    lap = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    sharpness = min(1.0, lap / 220.0)
    mean = float(gray.mean())
    std = float(gray.std())
    illumination = max(0.0, 1.0 - abs(mean - 128.0) / 128.0)
    contrast = min(1.0, std / 64.0)
    detection = float(face.det_score)

    frontal = 0.5
    kps = getattr(face, "kps", None)
    if kps is not None and len(kps) >= 3:
        left_eye, right_eye, nose = np.asarray(kps[:3], dtype=np.float32)
        eye_span = max(float(np.linalg.norm(right_eye - left_eye)), 1.0)
        eye_mid = (left_eye + right_eye) / 2.0
        yaw_offset = abs(float(nose[0] - eye_mid[0])) / eye_span
        roll = abs(float(left_eye[1] - right_eye[1])) / eye_span
        frontal = max(0.0, 1.0 - 1.8 * yaw_offset - 0.8 * roll)

    size_score = min(1.0, face_px / max(float(min_face_px * 2), 1.0))
    score = (
        0.24 * detection
        + 0.24 * sharpness
        + 0.16 * illumination
        + 0.10 * contrast
        + 0.16 * frontal
        + 0.10 * size_score
    )
    usable = face_px >= min_face_px and detection >= 0.55 and score >= min_quality
    return FaceQuality(
        round(score, 4), face_px, round(detection, 4), round(sharpness, 4),
        round(illumination, 4), round(contrast, 4), round(frontal, 4), usable,
    )
