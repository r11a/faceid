"""Persistent, per-camera operating profiles for the visual camera studio."""
import json
import threading
from pathlib import Path


ROLES = {"observation", "entry", "exit", "entry_exit", "restricted"}


class CameraProfiles:
    def __init__(self, path: Path, default_min_face_px: int = 48):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.default_min_face_px = int(default_min_face_px)
        self._lock = threading.RLock()

    def _read(self) -> dict:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def all(self) -> dict:
        with self._lock:
            return self._read()

    def get(self, camera: str) -> dict:
        stored = self.all().get(camera) or {}
        return {
            "camera": camera,
            "min_face_px": int(stored.get("min_face_px", self.default_min_face_px)),
            "role": stored.get("role", "observation"),
        }

    def update(self, camera: str, *, min_face_px: int, role: str) -> dict:
        camera = str(camera).strip()
        if not camera:
            raise ValueError("camera is required")
        min_face_px = int(min_face_px)
        if not 24 <= min_face_px <= 320:
            raise ValueError("min_face_px must be between 24 and 320")
        role = str(role)
        if role not in ROLES:
            raise ValueError("unknown camera role")
        with self._lock:
            profiles = self._read()
            profiles[camera] = {"min_face_px": min_face_px, "role": role}
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(profiles, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            temporary.replace(self.path)
        return self.get(camera)
