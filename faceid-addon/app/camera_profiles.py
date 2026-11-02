"""Persistent, per-camera operating profiles for the visual camera studio."""
import json
import hashlib
import threading
from pathlib import Path


ROLES = {"observation", "entry", "exit", "entry_exit", "restricted", "intercom"}
MODES = {"standard", "intercom"}
LIVENESS_MODES = {"off", "advisory", "required"}
RECOGNITION_MODES = {"event", "presence", "continuous"}


def _stored_seconds(value, default: float, minimum: float, maximum: float) -> float:
    try:
        seconds = float(value)
        return seconds if minimum <= seconds <= maximum else default
    except (TypeError, ValueError):
        return default


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
            return {name: {**{k:v for k,v in value.items() if k != 'direct_rtsp_url'},
                           'direct_rtsp_configured': bool(value.get('direct_rtsp_url'))}
                    for name,value in self._read().items() if isinstance(value,dict)}

    def get(self, camera: str) -> dict:
        stored = self.all().get(camera) or {}
        mode = stored.get("mode", "intercom" if stored.get("role") == "intercom" else "standard")
        recognition_mode = stored.get("recognition_mode", "event")
        if stored.get("observation_source", "snapshots") != "snapshots":
            recognition_mode = "event"
        return {
            "camera": camera,
            "observation_source": stored.get('observation_source','snapshots'),
            "trust_local_video_network": stored.get('trust_local_video_network',False) is True,
            "direct_rtsp_configured": stored.get('direct_rtsp_configured',False),
            "enabled": bool(stored.get("enabled", True)),
            "min_face_px": int(stored.get("min_face_px", self.default_min_face_px)),
            "role": stored.get("role", "observation"),
            "mode": mode if mode in MODES else "standard",
            "night_min_face_px": int(stored.get("night_min_face_px", stored.get("min_face_px", self.default_min_face_px))),
            "burst_frames": int(stored.get("burst_frames", 8)),
            "high_resolution": bool(stored.get("high_resolution", mode == "intercom")),
            "fast_recognition": bool(stored.get("fast_recognition", False)),
            "recognition_mode": recognition_mode if isinstance(recognition_mode, str) and recognition_mode in RECOGNITION_MODES else "event",
            "recognition_interval_seconds": _stored_seconds(stored.get("recognition_interval_seconds"), .5, .25, 5),
            "recognition_rearm_seconds": _stored_seconds(stored.get("recognition_rearm_seconds"), 2., 1, 30),
            "critical": bool(stored.get("critical", False)),
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
        fast_recognition: bool | None = None,
        critical: bool | None = None,
        recognition_mode: str | None = None,
        recognition_interval_seconds: float | None = None,
        recognition_rearm_seconds: float | None = None,
        observation_source: str | None = None,
        trust_local_video_network: bool | None = None,
        direct_rtsp_url: str | None = None,
        clear_direct_rtsp: bool = False,
    ) -> dict:
        camera = str(camera).strip()
        if not camera:
            raise ValueError("camera is required")
        if observation_source is not None and observation_source not in ('snapshots','frigate_restream','direct_rtsp'):
            raise ValueError('Invalid observation source')
        if trust_local_video_network is not None and type(trust_local_video_network) is not bool:
            raise ValueError('Local video trust must be boolean')
        if type(clear_direct_rtsp) is not bool:
            raise ValueError('Clear RTSP must be boolean')
        if direct_rtsp_url is not None:
            from .acquisition_diagnostics import validate_direct
            try: direct_rtsp_url=validate_direct(direct_rtsp_url)
            except ValueError: raise ValueError('Invalid private camera RTSP address') from None
        if clear_direct_rtsp and direct_rtsp_url is not None:
            raise ValueError('Cannot set and clear RTSP together')
        min_face_px = int(min_face_px)
        if not 24 <= min_face_px <= 320:
            raise ValueError("min_face_px must be between 24 and 320")
        role = str(role)
        if role not in ROLES:
            raise ValueError("unknown camera role")
        mode = str(mode)
        if mode not in MODES:
            raise ValueError("unknown camera mode")
        if recognition_mode is not None and (not isinstance(recognition_mode, str) or recognition_mode not in RECOGNITION_MODES):
            raise ValueError("unknown recognition mode")
        if recognition_interval_seconds is not None:
            recognition_interval_seconds = float(recognition_interval_seconds)
            if not .25 <= recognition_interval_seconds <= 5:
                raise ValueError("recognition_interval_seconds must be between 0.25 and 5")
        if recognition_rearm_seconds is not None:
            recognition_rearm_seconds = float(recognition_rearm_seconds)
            if not 1 <= recognition_rearm_seconds <= 30:
                raise ValueError("recognition_rearm_seconds must be between 1 and 30")
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
            saved_recognition_mode = stored.get("recognition_mode", "event")
            if not isinstance(saved_recognition_mode, str) or saved_recognition_mode not in RECOGNITION_MODES:
                saved_recognition_mode = "event"
            source=stored.get('observation_source','snapshots') if observation_source is None else observation_source
            direct=stored.get('direct_rtsp_url','') if direct_rtsp_url is None else direct_rtsp_url
            if clear_direct_rtsp: direct=''
            if source=='direct_rtsp' and not direct:
                raise ValueError('Configure a direct RTSP address or choose another source')
            profiles[camera] = {
                'observation_source':source,
                'trust_local_video_network':stored.get('trust_local_video_network',False) if trust_local_video_network is None else trust_local_video_network,
                'direct_rtsp_url':direct,
                "enabled": bool(stored.get("enabled", True) if enabled is None else enabled),
                "min_face_px": min_face_px, "role": role, "mode": mode,
                "night_min_face_px": night_min_face_px,
                "burst_frames": burst_frames,
                "high_resolution": bool(high_resolution),
                "fast_recognition": bool(stored.get("fast_recognition", False) if fast_recognition is None else fast_recognition),
                "recognition_mode": "event" if source != "snapshots" else (saved_recognition_mode if recognition_mode is None else recognition_mode),
                "recognition_interval_seconds": _stored_seconds(stored.get("recognition_interval_seconds"), .5, .25, 5) if recognition_interval_seconds is None else recognition_interval_seconds,
                "recognition_rearm_seconds": _stored_seconds(stored.get("recognition_rearm_seconds"), 2., 1, 30) if recognition_rearm_seconds is None else recognition_rearm_seconds,
                "critical": bool(stored.get("critical", False) if critical is None else critical),
                "require_second_factor": bool(require_second_factor),
                "liveness_mode": liveness_mode,
                "roi": roi,
            }
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(profiles, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            temporary.chmod(0o600)
            temporary.replace(self.path)
        return self.get(camera)

    def observation_config(self, camera):
        """Internal only: never return this credential-bearing record from an API."""
        with self._lock:
            stored=self._read().get(camera) or {}
            profile=self.get(camera)
            revision=hashlib.sha256(json.dumps(stored,sort_keys=True).encode()).hexdigest()
            return profile, stored.get('direct_rtsp_url',''), revision

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
            temporary.chmod(0o600)
            temporary.replace(self.path)
        return self.get(camera)
