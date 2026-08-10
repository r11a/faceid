"""Optional visual investigation path. Its output is never an identity decision."""
from __future__ import annotations

import base64
import json
import time
from pathlib import Path

import cv2
import numpy as np
import requests


class VisionAdvisor:
    def __init__(self, data_dir: Path, frame_distributor, *, enabled=False,
                 url="http://localhost:11434", model="gemma3:4b", timeout=60):
        self.enabled = bool(enabled)
        self.url = url.rstrip("/")
        self.model = model
        self.timeout = float(timeout)
        self.frames = frame_distributor
        self.audit_dir = data_dir / "vision_audit"
        self.audit_dir.mkdir(parents=True, exist_ok=True)
        self.last_error = None

    def candidate_grid(self, event_id: str, *, limit=12) -> Path | None:
        rows = self.frames.frames(event_id, limit=max(1, min(int(limit), 12)))
        if not rows:
            return None
        tiles = []
        for number, (_, image) in enumerate(rows, 1):
            tile = cv2.resize(image, (320, 240), interpolation=cv2.INTER_AREA)
            cv2.rectangle(tile, (0, 0), (56, 34), (31, 34, 37), -1)
            cv2.putText(tile, str(number), (12, 25), cv2.FONT_HERSHEY_SIMPLEX, .7, (220, 224, 229), 2)
            tiles.append(tile)
        blank = np.full_like(tiles[0], 30)
        while len(tiles) % 3:
            tiles.append(blank.copy())
        grid = np.vstack([np.hstack(tiles[i:i + 3]) for i in range(0, len(tiles), 3)])
        safe = self.frames.media_store._path(event_id).stem
        path = self.audit_dir / f"{safe}.jpg"
        cv2.imwrite(str(path), grid, [cv2.IMWRITE_JPEG_QUALITY, 88])
        return path

    def inspect(self, event_id: str, candidates: list[str]) -> dict:
        grid = self.candidate_grid(event_id)
        result = {"enabled": self.enabled, "advisory": True, "authority": "investigation-only",
                  "grid": grid.name if grid else None, "candidates": candidates[:5]}
        if not self.enabled:
            return {**result, "status": "disabled"}
        if grid is None:
            return {**result, "status": "no-media"}
        encoded = base64.b64encode(grid.read_bytes()).decode("ascii")
        prompt = (
            "You are reviewing numbered security-camera frames. Describe stable clothing and appearance "
            "signals only. Do not infer identity, age, gender, ethnicity, emotion or intent. "
            f"Possible labels supplied by other systems: {', '.join(candidates[:5]) or 'none'}. "
            "Return JSON with visible_clothing, consistency, useful_cells, limitations."
        )
        try:
            response = requests.post(f"{self.url}/api/chat", json={"model": self.model, "stream": False,
                "format": "json", "messages": [{"role": "user", "content": prompt,
                                                   "images": [encoded]}]}, timeout=self.timeout)
            response.raise_for_status()
            content = response.json().get("message", {}).get("content", "{}")
            analysis = json.loads(content)
            self.last_error = None
            return {**result, "status": "complete", "analysis": analysis, "created": time.time()}
        except Exception as exc:
            self.last_error = str(exc)[:240]
            return {**result, "status": "error", "error": self.last_error}

    def status(self):
        return {"enabled": self.enabled, "model": self.model, "authority": "investigation-only",
                "last_error": self.last_error}
