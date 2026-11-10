"""Subject association shared by live snapshots and bounded stream observation.

An event crop supplies only an identity anchor, never liveness or freshness
evidence. Gallery identity scores are deliberately absent from this module.
"""
import time

import numpy as np

from .face_tracks import FaceTracker
from .quality import measure_face_quality


def _diagnostics(diagnostics, **fields):
    # Passive optional output: a broken consumer cannot alter association.
    try:
        if isinstance(diagnostics, dict):
            diagnostics.update(fields)
    except Exception:
        pass


def usable_faces(image, faces, profile, *, min_face_px, min_quality,
                 apply_roi=True, hour=None, diagnostics=None):
    """Return (face, quality) pairs using the existing camera quality policy."""
    profile = profile or {}
    hour = time.localtime().tm_hour if hour is None else hour
    minimum = profile.get('night_min_face_px' if hour >= 19 or hour < 6
                          else 'min_face_px', min_face_px)
    height, width = image.shape[:2]
    result = []
    candidate_count = roi_rejected_count = quality_rejected_count = 0
    for face in faces:
        candidate_count += 1
        if apply_roi and profile.get('mode') == 'intercom':
            left, top, right, bottom = profile.get('roi', [0, 0, 1, 1])
            x, y = (np.asarray(face.bbox[:2]) + face.bbox[2:]) / 2
            if not (left <= x / max(width, 1) <= right
                    and top <= y / max(height, 1) <= bottom):
                roi_rejected_count += 1
                continue
        quality = measure_face_quality(image, face, min_face_px=minimum,
                                       min_quality=min_quality)
        if quality.usable:
            result.append((face, quality))
        else:
            quality_rejected_count += 1
    _diagnostics(diagnostics, candidate_count=candidate_count, usable_count=len(result),
                 roi_rejected_count=roi_rejected_count, quality_rejected_count=quality_rejected_count)
    return result


def single_anchor(engine, image, profile, *, min_face_px, min_quality, diagnostics=None):
    """Extract exactly one usable subject; crop-local coordinates have no ROI."""
    found = (engine.detect_faces(image) if hasattr(engine, 'detect_faces')
             else engine.faces(image))
    usable = usable_faces(image, found, profile, min_face_px=min_face_px,
                          min_quality=min_quality, apply_roi=False, diagnostics=diagnostics)
    if len(usable) != 1:
        _diagnostics(diagnostics, reason='anchor_face_ambiguous_or_low_quality')
        return None, 'anchor_face_ambiguous_or_low_quality'
    face = usable[0][0]
    if hasattr(engine, 'embed_face'):
        engine.embed_face(image, face)
    observation = FaceTracker._observation(face)
    if observation is None:
        _diagnostics(diagnostics, reason='anchor_embedding_invalid')
        return None, 'anchor_embedding_invalid'
    _diagnostics(diagnostics, reason='association_anchor_ready')
    return observation[1].copy(), 'association_anchor_ready'


def mutual_assignments(anchors, faces, *, diagnostics=None):
    """One-to-one anchor/face matches at cosine .65 and margin .08 both ways.

    Invalid dimensions or nonfinite vectors cannot contribute an assignment;
    input order never resolves a tie. The caller owns motion and history resets.
    """
    observations = [FaceTracker._observation(face) for face in faces]
    scores = np.full((len(anchors), len(faces)), -np.inf)
    for row, anchor in enumerate(anchors):
        try:
            anchor = np.asarray(anchor, dtype=float)
            norm = float(np.linalg.norm(anchor))
            if (anchor.ndim != 1 or not np.isfinite(anchor).all()
                    or not np.isfinite(norm) or norm <= 1e-12):
                continue
            anchor = anchor / norm
        except (TypeError, ValueError):
            continue
        for col, observation in enumerate(observations):
            if observation is not None and anchor.shape == observation[1].shape:
                scores[row, col] = float(np.dot(anchor, observation[1]))
    best = [FaceTracker._unique_best(row) for row in scores]
    reverse = [FaceTracker._unique_best(col) for col in scores.T]
    assignments = [col if col is not None and reverse[col] == row else None
                   for row, col in enumerate(best)]
    if diagnostics is not None:
        try:
            details = []
            for row, values in enumerate(scores):
                finite = sorted((float(value) for value in values if np.isfinite(value)), reverse=True)
                score = finite[0] if finite else None
                alternative = finite[1] if len(finite) > 1 else None
                col = int(np.argmax(values)) if score is not None else None
                other = ([float(value) for index, value in enumerate(scores[:, col])
                          if index != row and np.isfinite(value)] if col is not None else [])
                reason = ('associated' if assignments[row] is not None else
                          'no_usable_candidate' if not len(faces) else
                          'invalid_embedding' if score is None else
                          'anchor_similarity_below_threshold' if score < .65 else
                          'anchor_alternative_ambiguous' if best[row] is None else
                          'anchor_competition_ambiguous')
                details.append({'association_score': score, 'alternative_score': alternative,
                                'association_margin': score-alternative if alternative is not None else None,
                                'reverse_margin': score-max(other) if other and score is not None else None,
                                'reason': reason})
            _diagnostics(diagnostics, candidate_count=len(faces), anchor_count=len(anchors), assignments=details)
        except Exception:
            pass
    return assignments


def overlapping_indices(faces):
    """Return every face index involved in a positive-area box overlap."""
    affected = set()
    for index, face in enumerate(faces):
        a = np.asarray(face.bbox)
        for other_index in range(index + 1, len(faces)):
            b = np.asarray(faces[other_index].bbox)
            if np.all(np.minimum(a[2:], b[2:]) > np.maximum(a[:2], b[:2])):
                affected.update((index, other_index))
    return affected
