"""Safe face selection for photo enrollment.

An uploaded family photo must never silently enroll the largest bystander. This
module keeps that selection policy small and independently testable.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class EnrollmentSelection:
    face: object | None
    index: int | None
    reason: str
    candidates: list[dict]


def choose_enrollment_face(
    faces, reference_embeddings=None, *, requested_index: int | None = None,
    min_face_px: int = 60, auto_threshold: float = 0.45,
    auto_margin: float = 0.08,
) -> EnrollmentSelection:
    """Select one safe face or ask the UI for an explicit choice."""
    eligible = []
    for index, face in enumerate(faces or []):
        width = int(face.bbox[2] - face.bbox[0])
        height = int(face.bbox[3] - face.bbox[1])
        face_px = min(width, height)
        if face_px >= int(min_face_px):
            eligible.append((index, face, face_px))

    if requested_index is not None:
        selected = next((row for row in eligible if row[0] == requested_index), None)
        if selected is None:
            return EnrollmentSelection(None, None, "invalid_selection", [])
        return EnrollmentSelection(selected[1], selected[0], "user_selected", [])

    if not eligible:
        return EnrollmentSelection(None, None, "no_usable_face", [])
    if len(eligible) == 1:
        return EnrollmentSelection(eligible[0][1], eligible[0][0], "single_face", [])

    references = np.asarray(reference_embeddings) if reference_embeddings is not None else None
    scores = []
    if references is not None and references.ndim == 2 and len(references):
        for _, face, _ in eligible:
            scores.append(float(np.max(references @ face.normed_embedding)))
        order = np.argsort(scores)[::-1]
        best = int(order[0])
        runner_up = float(scores[int(order[1])]) if len(order) > 1 else 0.0
        if scores[best] >= auto_threshold and scores[best] - runner_up >= auto_margin:
            index, face, _ = eligible[best]
            return EnrollmentSelection(face, index, "clear_gallery_match", [])

    candidates = []
    for position, (index, face, face_px) in enumerate(eligible):
        candidates.append({
            "index": index,
            "bbox": [float(value) for value in face.bbox[:4]],
            "face_px": face_px,
            "match_score": round(scores[position], 3) if scores else None,
        })
    return EnrollmentSelection(None, None, "needs_selection", candidates)
