"""Persistent, per-camera operating profiles for the visual camera studio."""
import json
import threading
from pathlib import Path


ROLES = {"observation", "entry", "exit", "entry_exit", "restricted", "intercom"}
MODES = {"standard", "intercom"}
LIVENESS_MODES = {"off", "advisory", "required"}


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
        mode = stored.get("mode", "intercom" if stored.get("role") == "intercom" else "standard")
        return {
            "camera": camera,
            "enabled": bool(stored.get("enabled", True)),
            "min_face_px": int(stored.get("min_face_px", self.default_min_face_px)),
            "role": stored.get("role", "observation"),
            "mode": mode if mode in MODES else "standard",
            "night_min_face_px": int(stored.get("night_min_face_px", stored.get("min_face_px", self.default_min_face_px))),
            "burst_frames": int(stored.get("burst_frames", 8)),
            "high_resolution": bool(stored.get("high_resolution", mode == "intercom")),
            "require_second_factor": bool(stored.get("require_second_factor", mode == "intercom")),
            "liveness_mode": stored.get(
                "liveness_mode", "required" if mode == "intercom" else "advisory"
            ),
            "roi": stored.get("roi") if isinstance(stored.get("roi"), list) else [0.0, 0.0, 1.0, 1.0],
        }

    def update(
        self, camera: str, *, min_face_px: int, role: str,
        mode: str = "standard", night_min_face_px: int | None = None,
        burst_frames: int = 8, high_resolution: bool = False,
        require_second_factor: bool = True, liveness_mode: str | None = None,
        roi: list | None = None, enabled: bool | None = None,
    ) -> dict:
        camera = str(camera).strip()
        if not camera:
            raise ValueError("camera is required")
        min_face_px = int(min_face_px)
        if not 24 <= min_face_px <= 320:
            raise ValueError("min_face_px must be between 24 and 320")
        role = str(role)
        if role not in ROLES:
            raise ValueError("unknown camera role")
        mode = str(mode)
        if mode not in MODES:
            raise ValueError("unknown camera mode")
        liveness_mode = str(liveness_mode or ("required" if mode == "intercom" else "advisory"))
        if liveness_mode not in LIVENESS_MODES:
            raise ValueError("unknown liveness mode")
        night_min_face_px = int(night_min_face_px or min_face_px)
        if not 24 <= night_min_face_px <= 320:
            raise ValueError("night_min_face_px must be between 24 and 320")
        burst_frames = max(3, min(int(burst_frames), 30))
        roi = roi if isinstance(roi, list) and len(roi) == 4 else [0.0, 0.0, 1.0, 1.0]
        roi = [max(0.0, min(float(value), 1.0)) for value in roi]
        if roi[2] <= roi[0] or roi[3] <= roi[1]:
            raise ValueError("roi must describe a non-empty normalized rectangle")
        with self._lock:
            profiles = self._read()
            stored = profiles.get(camera) or {}
            profiles[camera] = {
                "enabled": bool(stored.get("enabled", True) if enabled is None else enabled),
                "min_face_px": min_face_px, "role": role, "mode": mode,
                "night_min_face_px": night_min_face_px,
                "burst_frames": burst_frames,
                "high_resolution": bool(high_resolution),
                "require_second_factor": bool(require_second_factor),
                "liveness_mode": liveness_mode,
                "roi": roi,
            }
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(profiles, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            temporary.replace(self.path)
        return self.get(camera)

    def set_enabled(self, camera: str, enabled: bool) -> dict:
        """Persist the automatic-processing switch without changing tuning."""
        camera = str(camera).strip()
        if not camera:
            raise ValueError("camera is required")
        with self._lock:
            profiles = self._read()
            stored = profiles.get(camera)
            profiles[camera] = dict(stored) if isinstance(stored, dict) else {}
            profiles[camera]["enabled"] = bool(enabled)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(profiles, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            temporary.replace(self.path)
        return self.get(camera)
