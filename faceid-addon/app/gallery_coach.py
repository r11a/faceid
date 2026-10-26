"""Explainable reference-gallery audit used by the learning wizard."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import threading

import cv2
import numpy as np


_cache_init_lock = threading.Lock()


def _file_signature(path: Path) -> tuple | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino


def _report_signature(gallery, people: dict) -> tuple:
    # Stat references on every request so replacements/removals are visible
    # immediately without decoding images or loading embedding arrays again.
    return (str(gallery.persons_dir), tuple(
        (
            slug, person["name"], tuple(person.get("files") or []),
            json.dumps(person.get("sources") or {}, sort_keys=True),
            tuple(_file_signature(gallery.persons_dir / slug / file)
                  for file in (person.get("files") or [])),
            _file_signature(gallery.persons_dir / slug / "embeddings.npy"),
            _file_signature(gallery.persons_dir / slug / "meta.json"),
        )
        for slug, person in people.items()
    ))


def _image_metrics(path: Path) -> tuple[float, float]:
    image = cv2.cvtColor(path, cv2.COLOR_BGR2GRAY) if isinstance(path, np.ndarray) else cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if image is None or image.size == 0:
        return 0.0, 0.0
    sharpness = min(1.0, float(cv2.Laplacian(image, cv2.CV_64F).var()) / 220.0)
    illumination = max(0.0, 1.0 - abs(float(image.mean()) - 128.0) / 128.0)
    return sharpness, illumination


def gallery_coach_report(gallery) -> dict:
    """Rank references by measurable clarity, exposure and added diversity."""
    # Keep one report on its owning gallery, not a process-wide identity cache.
    # The separate lock also coalesces concurrent UI polls without holding the
    # gallery's recognition/mutation lock during image processing.
    with _cache_init_lock:
        if not hasattr(gallery, "_coach_report_lock"):
            gallery._coach_report_lock = threading.Lock()
    with gallery._coach_report_lock:
        people = gallery.persons()
        signature = _report_signature(gallery, people)
        cached = getattr(gallery, "_coach_report_cache", None)
        if cached is not None and cached[0] == signature:
            return deepcopy(cached[1])
        gallery._coach_report_cache = None
        report = _build_report(gallery, people)
        # Do not retain a mixed snapshot if the gallery changed during the audit.
        if _report_signature(gallery, gallery.persons()) == signature:
            gallery._coach_report_cache = (signature, deepcopy(report))
        return report


def _build_report(gallery, people: dict) -> dict:
    reports = []
    for slug, person in people.items():
        files = list(person.get("files") or [])
        pdir = gallery.persons_dir / slug
        try:
            embeddings = np.load(pdir / "embeddings.npy")
        except (OSError, ValueError):
            embeddings = np.zeros((len(files), 512), dtype=np.float32)
        rows = []
        for index, file in enumerate(files):
            sharpness, illumination = _image_metrics(pdir / file)
            if len(files) > 1 and index < len(embeddings):
                similarities = embeddings @ embeddings[index]
                similarities[index] = -1.0
                nearest = float(similarities.max())
                novelty = min(1.0, max(0.0, (1.0 - nearest) / 0.35))
            else:
                nearest, novelty = 0.0, 1.0
            score = 0.45 * sharpness + 0.25 * illumination + 0.30 * novelty
            reasons = []
            if sharpness < 0.3:
                reasons.append("מטושטשת")
            if illumination < 0.35:
                reasons.append("תאורה קיצונית")
            if nearest > 0.94:
                reasons.append("דומה מאוד לתמונה אחרת")
            rows.append({
                "file": file, "url": f"media/persons/{slug}/{file}",
                "score": round(score, 3), "sharpness": round(sharpness, 3),
                "illumination": round(illumination, 3), "novelty": round(novelty, 3),
                "recommendation": "לבדיקה" if reasons else "מומלצת",
                "reasons": reasons,
            })
        cameras = sorted({
            source.get("camera") for source in (person.get("sources") or {}).values()
            if source.get("camera")
        })
        advice = []
        if len(files) < 5:
            advice.append("הוסיפו לפחות 5 תמונות ברורות")
        if len(cameras) < 2:
            advice.append("הוסיפו תמונות ממצלמה או זווית נוספת")
        if sum(bool(row["reasons"]) for row in rows) > max(1, len(rows) // 4):
            advice.append("עברו על התמונות שסומנו לבדיקה")
        reports.append({
            "slug": slug, "person": person["name"], "images": rows,
            "cameras": cameras, "advice": advice or ["הכיסוי נראה מאוזן"],
            "review_count": sum(bool(row["reasons"]) for row in rows),
        })
    return {
        "people": reports,
        "summary": {
            "people": len(reports),
            "images": sum(len(report["images"]) for report in reports),
            "review": sum(report["review_count"] for report in reports),
        },
        "method": (
            "הציון מסביר חדות, תאורה וגיוון מול שאר התמונות. הוא מסייע לבחירה "
            "ואינו משנה את הגלריה בעצמו."
        ),
    }
