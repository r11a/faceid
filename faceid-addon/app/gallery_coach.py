"""Explainable reference-gallery audit used by the learning wizard."""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np


def _image_metrics(path: Path) -> tuple[float, float]:
    image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if image is None or image.size == 0:
        return 0.0, 0.0
    sharpness = min(1.0, float(cv2.Laplacian(image, cv2.CV_64F).var()) / 220.0)
    illumination = max(0.0, 1.0 - abs(float(image.mean()) - 128.0) / 128.0)
    return sharpness, illumination


def gallery_coach_report(gallery) -> dict:
    """Rank references by measurable clarity, exposure and added diversity."""
    people = gallery.persons()
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
