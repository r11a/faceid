"""Temporary guest identities and fail-closed access eligibility decisions."""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
import uuid
from pathlib import Path

import cv2
import numpy as np


class GuestAccess:
    def __init__(self, data_dir: Path, *, threshold: float = .62, margin: float = .12):
        self.root = data_dir / "guests"
        self.root.mkdir(parents=True, exist_ok=True)
        self.events_path = data_dir / "guest_access.json"
        self.threshold = max(.5, float(threshold))
        self.margin = max(.08, float(margin))
        self._lock = threading.RLock()

    @staticmethod
    def _atomic_json(path: Path, value) -> None:
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2)
                handle.flush(); os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _meta(self, guest_id: str) -> dict | None:
        path = self.root / guest_id / "meta.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else None
        except (OSError, json.JSONDecodeError):
            return None

    @staticmethod
    def status(meta: dict, now: float | None = None) -> str:
        now = float(now or time.time())
        if meta.get("revoked"):
            return "revoked"
        if now < float(meta.get("valid_from") or 0):
            return "future"
        if now > float(meta.get("valid_until") or 0):
            return "expired"
        maximum = int(meta.get("max_entries") or 0)
        if maximum and int(meta.get("entries_used") or 0) >= maximum:
            return "used"
        return "active"

    def list(self) -> list[dict]:
        with self._lock:
            result = []
            for folder in self.root.iterdir():
                if not folder.is_dir():
                    continue
                meta = self._meta(folder.name)
                if not meta:
                    continue
                result.append({**meta, "status": self.status(meta), "photo": f"media/guests/{folder.name}/face.jpg"})
            return sorted(result, key=lambda item: (item["status"] != "active", item.get("valid_from", 0)))

    def create(self, *, name: str, valid_from: float, valid_until: float,
               max_entries: int, allowed_cameras: list[str], crop, embedding) -> dict:
        name = str(name).strip()
        if not name or len(name) > 100:
            raise ValueError("guest name must contain 1-100 characters")
        if float(valid_until) <= float(valid_from):
            raise ValueError("valid_until must be after valid_from")
        if float(valid_until) - float(valid_from) > 90 * 86400:
            raise ValueError("guest access may not exceed 90 days")
        guest_id = uuid.uuid4().hex[:12]
        folder = self.root / guest_id
        folder.mkdir()
        meta = {
            "id": guest_id, "name": name, "valid_from": float(valid_from),
            "valid_until": float(valid_until), "max_entries": max(1, min(int(max_entries), 1000)),
            "entries_used": 0, "allowed_cameras": sorted({str(x) for x in allowed_cameras if str(x)}),
            "revoked": False, "created_ts": time.time(),
        }
        try:
            if not cv2.imwrite(str(folder / "face.jpg"), crop, [cv2.IMWRITE_JPEG_QUALITY, 92]):
                raise ValueError("could not store guest face")
            with (folder / "embedding.npy").open("wb") as handle:
                np.save(handle, np.asarray(embedding, dtype=np.float32))
                handle.flush(); os.fsync(handle.fileno())
            self._atomic_json(folder / "meta.json", meta)
        except Exception:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        return {**meta, "status": self.status(meta), "photo": f"media/guests/{guest_id}/face.jpg"}

    def revoke(self, guest_id: str) -> dict:
        with self._lock:
            meta = self._meta(guest_id)
            if not meta:
                raise KeyError(guest_id)
            meta["revoked"] = True
            self._atomic_json(self.root / guest_id / "meta.json", meta)
            return {**meta, "status": self.status(meta)}

    def delete(self, guest_id: str) -> None:
        with self._lock:
            folder = (self.root / guest_id).resolve()
            if folder.parent != self.root.resolve() or not folder.is_dir():
                raise KeyError(guest_id)
            shutil.rmtree(folder)

    def candidates(self, embedding, *, camera: str, now: float | None = None, limit: int = 2) -> list[tuple[str, str, float]]:
        now = float(now or time.time())
        rows = []
        with self._lock:
            for meta in self.list():
                if self.status(meta, now) != "active":
                    continue
                allowed = meta.get("allowed_cameras") or []
                if allowed and camera not in allowed:
                    continue
                try:
                    reference = np.load(self.root / meta["id"] / "embedding.npy")
                    score = float(np.asarray(reference, dtype=np.float32) @ np.asarray(embedding, dtype=np.float32))
                except (OSError, ValueError):
                    continue
                rows.append((f"guest:{meta['id']}", meta["name"], score))
        return sorted(rows, key=lambda item: item[2], reverse=True)[:max(1, limit)]

    def evaluate(self, guest_id: str, *, camera: str, score: float, runner_up_score: float,
                 liveness_confirmed: bool, second_factor: bool, event_id: str | None = None) -> dict:
        """Return and audit eligibility. Only a complete decision consumes an entry."""
        with self._lock:
            meta = self._meta(guest_id)
            if not meta:
                raise KeyError(guest_id)
            reasons = []
            if self.status(meta) != "active": reasons.append("guest_not_active")
            if meta.get("allowed_cameras") and camera not in meta["allowed_cameras"]: reasons.append("camera_not_allowed")
            if float(score) < self.threshold: reasons.append("score_too_low")
            if float(score) - float(runner_up_score) < self.margin: reasons.append("margin_too_low")
            if not liveness_confirmed: reasons.append("liveness_required")
            if not second_factor: reasons.append("second_factor_required")
            authorized = not reasons
            if authorized:
                meta["entries_used"] = int(meta.get("entries_used") or 0) + 1
                self._atomic_json(self.root / guest_id / "meta.json", meta)
            event = {
                "id": uuid.uuid4().hex, "guest_id": guest_id, "guest_name": meta["name"],
                "event_id": event_id, "camera": camera, "score": round(float(score), 4),
                "authorized": authorized, "reasons": reasons, "ts": time.time(),
            }
            try:
                history = json.loads(self.events_path.read_text(encoding="utf-8"))
                if not isinstance(history, list): history = []
            except (OSError, json.JSONDecodeError):
                history = []
            self._atomic_json(self.events_path, [event, *history[:999]])
            return {**event, "entries_used": meta["entries_used"], "entries_left": max(0, int(meta["max_entries"]) - int(meta["entries_used"]))}

    def history(self, limit: int = 100) -> list[dict]:
        try:
            rows = json.loads(self.events_path.read_text(encoding="utf-8"))
            return rows[:max(1, min(int(limit), 1000))] if isinstance(rows, list) else []
        except (OSError, json.JSONDecodeError):
            return []
