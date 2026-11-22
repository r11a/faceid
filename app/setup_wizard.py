"""6.9.0 setup backend: discovery, stream probing, recommendations, geometry checks, journaled apply.

One authoritative copy of each setting:
  * settings.json         - FaceID tuning overlay (existing; part of the gallery backup)
  * camera_profiles.json  - per-camera settings, incl. optional stream decode (existing)
  * connection.json       - Frigate/MQTT connection set from the UI; secrets, 0600,
                            never in the downloadable backup, never returned by an API
  * setup.json            - schema version, setup state, geometry fingerprints only
Precedence: connection.json / settings.json > add-on options > defaults.
"""
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

log = logging.getLogger("faceid.setup")

SCHEMA_VERSION = 1
SETUP_FILE = "setup.json"
CONNECTION_FILE = "connection.json"
SETTINGS_FILE = "settings.json"
PROFILES_FILE = "camera_profiles.json"
JOURNAL_FILE = "setup-journal.json"
BACKUP_DIR = "setup-backup"
KEY_FILE = "setup.key"
APPLY_FILES = (SETTINGS_FILE, PROFILES_FILE, CONNECTION_FILE, SETUP_FILE)

TARGET_FPS = 8
MAX_DECODE_WIDTH = 1920
LOW_RESOLUTION_WIDTH = 1280
PROBE_TIMEOUT = 10
VAAPI_DEVICE = "/dev/dri/renderD128"
GPU_CODECS = {"h264", "hevc"}
CONNECTION_FIELDS = {
    "frigate": ("url", "username", "password", "verify_tls"),
    "mqtt": ("host", "port", "user", "password"),
}
SECRET_FIELDS = {"password"}
SETTINGS_FIELDS = {"stream_warm_seconds": (0, 3600)}
GEOMETRY_FIELDS = ("source", "restream", "detect", "stream", "source_digest")
CHANGE_TEXT = {
    "source": "FaceID video source changed",
    "restream": "Frigate restream for this camera changed",
    "detect": "Frigate detect resolution changed",
    "stream": "Camera stream resolution changed",
    "source_digest": "Frigate camera input or restream source changed",
}
PAUSED_TEXT = ("Frigate box association is paused until this camera's view is checked again. "
               "Recognition still works, but needs a stored-face match, so it may take longer.")

_files_lock = threading.RLock()
_probe_lock = threading.Lock()


# ---------- files ----------

def _write_json(path: Path, value) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=1)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.chmod(0o600)
    temporary.replace(path)


def _read_json(path: Path, default=None):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else default
    except (OSError, ValueError):
        return default


def _copy_private(source: Path, target: Path) -> None:
    temporary = target.with_name(target.name + ".tmp")
    shutil.copyfile(source, temporary)
    temporary.chmod(0o600)
    temporary.replace(target)


# ---------- journaled apply / recovery / rollback ----------

def _restore(data_dir: Path, files: dict) -> None:
    backup = data_dir / BACKUP_DIR
    for name, present in files.items():
        if name not in APPLY_FILES:
            continue
        target = data_dir / name
        if present:
            _copy_private(backup / name, target)
        elif target.exists():
            target.unlink()


def transaction(data_dir: Path, writes: dict, *, after_write=None) -> None:
    """Replace several setup files as one recoverable operation.

    The previous files are copied to setup-backup/ (kept for one-step rollback) and a
    journal is written before any target changes; an unfinished journal is undone at
    the next start by recover_interrupted_apply().
    """
    if not writes or any(name not in APPLY_FILES for name in writes):
        raise ValueError("unknown setup file")
    backup = data_dir / BACKUP_DIR
    with _files_lock:
        recover_interrupted_apply(data_dir)
        shutil.rmtree(backup, ignore_errors=True)
        backup.mkdir(mode=0o700)
        manifest = {}
        for name in writes:
            source = data_dir / name
            manifest[name] = source.exists()
            if manifest[name]:
                _copy_private(source, backup / name)
        _write_json(backup / "manifest.json", manifest)
        _write_json(data_dir / JOURNAL_FILE, {"started": time.time(), "files": manifest})
        for name, value in writes.items():
            _write_json(data_dir / name, value)
            if after_write:
                after_write(name)
        (data_dir / JOURNAL_FILE).unlink()


def recover_interrupted_apply(data_dir: Path) -> bool:
    """Undo a setup apply or rollback that did not finish (crash, power loss)."""
    journal = data_dir / JOURNAL_FILE
    with _files_lock:
        if not journal.exists():
            return False
        files = (_read_json(journal) or {}).get("files")
        if not isinstance(files, dict):
            files = _read_json(data_dir / BACKUP_DIR / "manifest.json")
        if isinstance(files, dict):
            _restore(data_dir, files)
            log.warning("setup: an interrupted apply was found and the previous settings were restored")
        else:
            log.error("setup: interrupted apply found without a readable backup; files left as they are")
        journal.unlink()
        return True


def rollback_available(data_dir: Path) -> bool:
    return isinstance(_read_json(data_dir / BACKUP_DIR / "manifest.json"), dict)


def rollback(data_dir: Path) -> dict:
    """Restore the files as they were before the last apply (one step)."""
    backup = data_dir / BACKUP_DIR
    with _files_lock:
        recover_interrupted_apply(data_dir)
        manifest = _read_json(backup / "manifest.json")
        if not isinstance(manifest, dict):
            raise LookupError("nothing to roll back")
        _write_json(data_dir / JOURNAL_FILE, {"started": time.time(), "files": manifest, "rollback": True})
        _restore(data_dir, manifest)
        (data_dir / JOURNAL_FILE).unlink()
        shutil.rmtree(backup, ignore_errors=True)
    return {"restored": sorted(manifest)}


# ---------- connection ----------

def valid_url(value) -> bool:
    if not isinstance(value, str) or len(value) > 512:
        return False
    parts = urlsplit(value)
    return parts.scheme in ("http", "https") and bool(parts.hostname)


def _clean_connection(section: str, values) -> dict:
    if section not in CONNECTION_FIELDS or not isinstance(values, dict):
        raise ValueError(f"invalid {section} connection")
    if set(values) - set(CONNECTION_FIELDS[section]):
        raise ValueError(f"unknown {section} connection field")
    clean = {}
    for key, value in values.items():
        if key == "url":
            if not valid_url(value):
                raise ValueError("Frigate address must be an http(s) URL")
            clean[key] = value.rstrip("/")
        elif key == "verify_tls":
            if type(value) is not bool:
                raise ValueError("verify_tls must be true or false")
            clean[key] = value
        elif key == "host":
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9.:\[\]_-]{1,253}", value):
                raise ValueError("invalid MQTT host")
            clean[key] = value
        elif key == "port":
            if type(value) is not int or not 1 <= value <= 65535:
                raise ValueError("invalid MQTT port")
            clean[key] = value
        else:
            if not isinstance(value, str) or len(value) > 256:
                raise ValueError(f"invalid {section} {key}")
            clean[key] = value
    return clean


def merge_connection(cfg: dict, data_dir: Path) -> dict:
    """Apply connection.json over the add-on options; returns which sections came from it."""
    stored = _read_json(data_dir / CONNECTION_FILE, {}) or {}
    used = {}
    for section, fields in CONNECTION_FIELDS.items():
        values = stored.get(section)
        if not isinstance(values, dict):
            continue
        target = cfg.setdefault(section, {})
        for key in fields:
            if key in values and values[key] not in (None, ""):
                target[key] = values[key]
                used[section] = True
    return used


def missing_connections(cfg: dict) -> list:
    """Connections FaceID cannot operate without; FaceID then starts in setup mode."""
    missing = []
    if not valid_url((cfg.get("frigate") or {}).get("url")):
        missing.append("frigate")
    if not (cfg.get("mqtt") or {}).get("host"):
        missing.append("mqtt")
    return missing


def redacted_connection(cfg: dict) -> dict:
    frigate, mqtt = cfg.get("frigate") or {}, cfg.get("mqtt") or {}
    return {
        "frigate": {"url": frigate.get("url") or "", "verify_tls": bool(frigate.get("verify_tls", True)),
                    "username_set": bool(frigate.get("username")),
                    "password_set": bool(frigate.get("password"))},
        "mqtt": {"host": mqtt.get("host") or "", "port": int(mqtt.get("port") or 1883),
                 "user_set": bool(mqtt.get("user")), "password_set": bool(mqtt.get("password"))},
    }


# ---------- setup state ----------

def existing_install(data_dir: Path) -> bool:
    if any((data_dir / name).exists() for name in ("audit.db", PROFILES_FILE, SETTINGS_FILE)):
        return True
    persons = data_dir / "persons"
    return persons.is_dir() and any(persons.iterdir())


class SetupState:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.path = data_dir / SETUP_FILE

    def initialize(self, existing: bool) -> dict:
        """First start of 6.9.0: an upgrade keeps working as before, a blank install needs setup."""
        with _files_lock:
            if not self.path.exists():
                self.data_dir.mkdir(parents=True, exist_ok=True)
                _write_json(self.path, {"schema_version": SCHEMA_VERSION,
                                        "state": "migrated" if existing else "fresh",
                                        "created_at": time.time(), "cameras": {}})
                log.info("setup: %s", "existing installation kept as configured" if existing
                         else "blank installation - open FaceID from the Home Assistant sidebar to set it up")
            return self.read()

    def read(self) -> dict:
        value = _read_json(self.path, {}) or {}
        value.setdefault("schema_version", SCHEMA_VERSION)
        value.setdefault("state", "fresh")
        if not isinstance(value.get("cameras"), dict):
            value["cameras"] = {}
        return value

    def update(self, change) -> dict:
        with _files_lock:
            value = self.read()
            change(value)
            _write_json(self.path, value)
            return value


# ---------- stream probe and recommendation ----------

def _rate(value):
    try:
        top, _, bottom = str(value).partition("/")
        rate = float(top) / float(bottom or 1)
        return round(rate, 2) if 1 <= rate <= 120 else None
    except (ValueError, ZeroDivisionError):
        return None


def parse_probe(output) -> dict:
    data = json.loads(output)
    stream = (data.get("streams") or [None])[0]
    if not isinstance(stream, dict):
        raise ValueError("no video stream")
    width, height = stream.get("width"), stream.get("height")
    if type(width) is not int or type(height) is not int or not (16 <= width <= 16384 and 16 <= height <= 16384):
        raise ValueError("invalid size")
    codec = str(stream.get("codec_name") or "").lower()[:32]
    fps = _rate(stream.get("avg_frame_rate")) or _rate(stream.get("r_frame_rate"))
    return {"ok": True, "codec": codec, "width": width, "height": height, "fps": fps}


def probe_stream(url, *, timeout=PROBE_TIMEOUT, run=subprocess.run) -> dict:
    """Codec, size and frame rate of the exact restream FaceID decodes.

    Bounded: one probe at a time, hard timeout, video stream facts only. The URL and
    FFprobe's own messages are never logged or returned.
    """
    if not url:
        return {"ok": False, "reason": "no_restream"}
    if not _probe_lock.acquire(timeout=timeout):
        return {"ok": False, "reason": "busy"}
    try:
        result = run(["ffprobe", "-v", "error", "-rtsp_transport", "tcp", "-select_streams", "v:0",
                      "-show_entries", "stream=codec_name,width,height,avg_frame_rate,r_frame_rate",
                      "-of", "json", url],
                     stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout, check=False)
        if result.returncode != 0:
            return {"ok": False, "reason": "unreachable"}
        return parse_probe(result.stdout)
    except FileNotFoundError:
        return {"ok": False, "reason": "ffprobe_missing"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": "timeout"}
    except (ValueError, TypeError, AttributeError):
        return {"ok": False, "reason": "unreadable"}
    finally:
        _probe_lock.release()


def gpu_available() -> bool:
    return os.path.exists(VAAPI_DEVICE)


def recommend(facts: dict, *, gpu=None) -> dict:
    """Recommended decode for a stream; the operator confirms it before it is applied."""
    gpu = gpu_available() if gpu is None else gpu
    width, fps = facts.get("width"), facts.get("fps")
    codec = str(facts.get("codec") or "").lower()
    if type(width) is not int or width <= 0:
        raise ValueError("stream width is required")
    step = max(1, min(10, int(fps / TARGET_FPS + 0.5))) if fps else 3
    result = {"gpu_decode": bool(gpu and codec in GPU_CODECS),
              "decode_width": MAX_DECODE_WIDTH if width > MAX_DECODE_WIDTH else 0,
              "decode_step": step,
              "delivered_fps": round(fps / step, 1) if fps else None,
              "notes": []}
    if width < LOW_RESOLUTION_WIDTH:
        result["notes"].append("low_resolution")  # substream: faces must come close
    if codec not in GPU_CODECS:
        result["notes"].append("cpu_codec")
    elif not gpu:
        result["notes"].append("no_gpu")
    return result


# ---------- Frigate discovery ----------

def restream_alias(name, camera: dict, streams: dict):
    """Same choice as FrigateAPI._main_stream: live alias, else the camera-named restream."""
    configured = ((camera or {}).get("live") or {}).get("streams") or {}
    choices = list(configured.values()) if isinstance(configured, dict) else []
    if not choices:
        choices = [name]
    return next((value for value in choices if isinstance(value, str) and value in (streams or {})
                 and re.fullmatch(r"[A-Za-z0-9_.-]+", value) and value not in (".", "..")), None)


def frigate_cameras(config: dict, enabled=lambda camera: True) -> list:
    streams = ((config or {}).get("go2rtc") or {}).get("streams") or {}
    cameras = []
    for name, camera in sorted(((config or {}).get("cameras") or {}).items()):
        if not isinstance(camera, dict):
            continue
        detect = camera.get("detect") or {}
        track = ((camera.get("objects") or {}).get("track")) or []
        alias = restream_alias(name, camera, streams)
        warnings = []
        if not alias:
            warnings.append("no_restream")
        if camera.get("enabled") is False:
            warnings.append("camera_disabled")
        if (camera.get("snapshots") or {}).get("enabled") is False:
            warnings.append("snapshots_disabled")
        if "person" not in track:
            warnings.append("person_not_tracked")
        cameras.append({"camera": name, "restream": alias,
                        "detect": {key: detect.get(key) for key in ("width", "height", "fps")},
                        "used_by_faceid": bool(enabled(name)), "warnings": warnings})
    return cameras


def discover(cfg: dict, frigate, engine=None, enabled=lambda camera: True) -> dict:
    connection = redacted_connection(cfg)
    config = frigate.config() if frigate is not None and valid_url(connection["frigate"]["url"]) else None
    frigate_prefix = (((config or {}).get("mqtt") or {}).get("topic_prefix")) if config else None
    expected_prefix = (cfg.get("faceid") or {}).get("frigate_topic_prefix") or "frigate"
    return {
        "system": {"gpu_device": gpu_available(),
                   "backend": getattr(engine, "active_backend", None),
                   "native_device": getattr(engine, "native_device", None)},
        "mqtt": {**connection["mqtt"], "configured": bool(connection["mqtt"]["host"])},
        "frigate": {**connection["frigate"], "configured": valid_url(connection["frigate"]["url"]),
                    "reachable": config is not None,
                    "topic_prefix": frigate_prefix,
                    "topic_prefix_matches": frigate_prefix in (None, expected_prefix),
                    "cameras": frigate_cameras(config, enabled) if config else []},
        "manual_fallback": config is None,
    }


# ---------- geometry ----------

def _site_key(data_dir: Path) -> bytes:
    path = data_dir / KEY_FILE
    with _files_lock:
        try:
            key = path.read_bytes()
            if len(key) == 32:
                return key
        except OSError:
            pass
        key = secrets.token_bytes(32)
        temporary = path.with_name(KEY_FILE + ".tmp")
        temporary.write_bytes(key)
        temporary.chmod(0o600)
        temporary.replace(path)
        return key


def source_digest(key: bytes, camera: dict, alias, streams: dict, direct_url="") -> str:
    """Keyed digest of the camera's inputs and restream source; no address is stored."""
    inputs = [{"path": item.get("path"), "roles": item.get("roles"), "input_args": item.get("input_args")}
              for item in (((camera or {}).get("ffmpeg") or {}).get("inputs") or []) if isinstance(item, dict)]
    material = json.dumps({"inputs": inputs, "restream": (streams or {}).get(alias) if alias else None,
                           "direct": direct_url or ""}, sort_keys=True, default=str)
    return hmac.new(key, material.encode(), hashlib.sha256).hexdigest()[:24]


def geometry_changes(validated: dict, current: dict) -> list:
    return [key for key in GEOMETRY_FIELDS
            if validated.get(key) is not None and current.get(key) is not None
            and validated[key] != current[key]]


class GeometryMonitor:
    """Watches every Frigate camera's source and geometry against what was validated.

    One read of Frigate's configuration covers all cameras, on or off; only cameras
    FaceID uses have their stream probed. A change pauses Frigate box association for
    that camera (anchor match only) until the operator checks the view again. An
    upgraded installation accepts every camera it finds on its first full check, so a
    camera turned on later behaves exactly as before the upgrade.
    """

    def __init__(self, data_dir: Path, state: SetupState, frigate, profiles, *,
                 enabled=lambda camera: True, busy=lambda: False, probe=probe_stream,
                 interval=600.0, first_delay=30.0):
        self.data_dir, self.state, self.frigate, self.profiles = data_dir, state, frigate, profiles
        self.enabled, self.busy, self.probe = enabled, busy, probe
        self.interval, self.first_delay = interval, first_delay
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._allowed = {}
        self._refresh_allowed(state.read())

    def _decide(self, value: dict, camera: str) -> bool:
        entry = value["cameras"].get(camera) or {}
        if entry.get("validated"):
            return not entry.get("changes")
        # Never validated: an upgrade keeps 6.8.7 behaviour until its first full check.
        return upgrade_pending(value)

    def _refresh_allowed(self, value: dict) -> None:
        with self._lock:
            self._allowed = {camera: self._decide(value, camera) for camera in value["cameras"]}
            self._default = self._decide(value, "\0")

    def box_association_allowed(self, camera: str) -> bool:
        with self._lock:
            return self._allowed.get(camera, self._default)

    def _facts(self, camera, config, key, probe):
        streams = ((config.get("go2rtc") or {}).get("streams")) or {}
        frigate_camera = (config.get("cameras") or {}).get(camera) or {}
        detect = frigate_camera.get("detect") or {}
        alias = restream_alias(camera, frigate_camera, streams)
        stored, direct, _ = self.profiles.observation_config(camera) if self.profiles else ({}, "", None)
        source = stored.get("observation_source", "snapshots")
        facts = {"source": source, "restream": alias,
                 "detect": [detect.get("width"), detect.get("height")]
                 if type(detect.get("width")) is int and type(detect.get("height")) is int else None,
                 "stream": None,
                 "source_digest": source_digest(key, frigate_camera, alias, streams,
                                                direct if source == "direct_rtsp" else "")}
        if probe and alias and self.enabled(camera):
            result = self.probe(self.frigate.live_url(camera))
            if result.get("ok"):
                facts["stream"] = [result["width"], result["height"]]
        return facts

    def check(self, cameras=None, *, probe=True) -> dict | None:
        config = self.frigate.config() if self.frigate is not None else None
        if not isinstance(config, dict):
            return None
        key = _site_key(self.data_dir)
        names = list(config.get("cameras") or {})
        if cameras is not None:
            names = [name for name in names if name in cameras]
        observed = {name: self._facts(name, config, key, probe) for name in names}
        now = time.time()

        def record(value):
            # Only a full check of every camera completes the upgrade hand-over.
            accept = cameras is None and upgrade_pending(value)
            for name, facts in observed.items():
                entry = value["cameras"].setdefault(name, {})
                entry["current"], entry["checked_at"] = facts, now
                validated = entry.get("validated")
                if not validated and accept:
                    entry["validated"], entry["validated_at"], entry["validated_by"] = facts, now, "upgrade"
                    validated = facts
                if not validated:
                    entry["changes"] = []
                    continue
                changes = geometry_changes(validated, facts)
                if changes and changes != entry.get("changes"):
                    log.warning("setup: camera %s %s; %s", name,
                                "; ".join(CHANGE_TEXT[item] for item in changes), PAUSED_TEXT)
                elif not changes and entry.get("changes"):
                    log.info("setup: camera %s matches its validated geometry again", name)
                if not changes:
                    # Fill facts the first check could not observe (e.g. probe unavailable).
                    validated.update({k: v for k, v in facts.items() if validated.get(k) is None})
                entry["changes"] = changes
            if accept:
                value["migration_checked"] = value["upgrade_all_cameras"] = True

        value = self.state.update(record)
        self._refresh_allowed(value)
        return value

    def accept(self, camera: str) -> dict:
        """Operator confirmed the view: the current geometry becomes the validated one."""
        if self.check([camera]) is None:
            raise LookupError("Frigate configuration unavailable")
        def mark(value):
            entry = value["cameras"].get(camera)
            if not entry or not entry.get("current"):
                raise LookupError("camera not found in Frigate")
            entry.update(validated=dict(entry["current"]), validated_at=time.time(),
                         validated_by="operator", changes=[])
        value = self.state.update(mark)
        self._refresh_allowed(value)
        return camera_status(value, camera)

    def _run(self):
        if self._stop.wait(self.first_delay):
            return
        while not self._stop.is_set():
            if self.busy():
                self._stop.wait(30)
                continue
            try:
                self.check()
            except Exception:
                log.exception("setup: geometry check failed; previous decisions kept")
            self._stop.wait(self.interval)

    def start(self):
        threading.Thread(target=self._run, daemon=True, name="faceid-setup-geometry").start()

    def stop(self):
        self._stop.set()


def upgrade_pending(value: dict) -> bool:
    """An upgraded installation whose cameras have not all been accepted yet.

    6.9.0 accepted only the cameras in use; 6.9.1 accepts the rest on its first check.
    """
    return value.get("state") == "migrated" and not value.get("upgrade_all_cameras")


def camera_status(value: dict, camera: str) -> dict:
    entry = value["cameras"].get(camera) or {}
    changes = list(entry.get("changes") or [])
    if entry.get("validated"):
        status = "changed" if changes else "validated"
    elif upgrade_pending(value):
        status = "pending"
    else:
        status = "unvalidated"
    return {"camera": camera, "status": status,
            "box_association": status in ("validated", "pending"),
            "changes": [{"field": item, "text": CHANGE_TEXT[item],
                         "was": (entry.get("validated") or {}).get(item),
                         "now": (entry.get("current") or {}).get(item) if item != "source_digest" else None}
                        for item in changes],
            "message": PAUSED_TEXT if status in ("changed", "unvalidated") else None,
            "validated_at": entry.get("validated_at"), "checked_at": entry.get("checked_at")}


# ---------- walk test ----------

class WalkTest:
    """Setup walk test: shows recognition and liveness results; Access1 is never asked.

    While a test runs on a camera, a recognized decision there is recorded here and
    its Access1 request is skipped. The test ends by itself after at most 5 minutes.
    """
    MAX_RESULTS = 20

    def __init__(self, clock=time.time):
        self._clock = clock
        self._lock = threading.Lock()
        self._camera, self._started, self._until, self._results = None, 0.0, 0.0, []

    def start(self, camera: str, seconds: int = 120) -> dict:
        if not isinstance(camera, str) or not camera.strip():
            raise ValueError("camera is required")
        if type(seconds) is not int or not 10 <= seconds <= 300:
            raise ValueError("seconds must be between 10 and 300")
        with self._lock:
            now = self._clock()
            self._camera, self._started, self._until, self._results = camera, now, now + seconds, []
        log.info("setup walk test started on %s for %d s: Access1 requests are not sent", camera, seconds)
        return self.status()

    def stop(self) -> dict:
        with self._lock:
            self._until = 0.0
        return self.status()

    def active(self, camera) -> bool:
        with self._lock:
            return camera == self._camera and self._clock() < self._until

    def intercept(self, event_id, st: dict, decision) -> bool:
        """True when this recognition belongs to the walk test (no Access1 request)."""
        camera = st.get("camera")
        if not self.active(camera):
            return False
        liveness = st.get("liveness") or {}
        started = st.get("start_time")
        now = self._clock()
        result = {"event_id": event_id, "person": getattr(decision, "person", None),
                  "score": getattr(decision, "score", None), "margin": getattr(decision, "margin", None),
                  "liveness_confirmed": bool(liveness.get("confirmed")),
                  "liveness_score": liveness.get("score"), "live_frames": liveness.get("live_frames", 0),
                  "recognized_after_seconds": round(now - started, 2)
                  if isinstance(started, (int, float)) and 0 <= now - started < 3600 else None,
                  "at": now}
        with self._lock:
            self._results = (self._results + [result])[-self.MAX_RESULTS:]
        log.info("setup walk test: %s recognized on %s; Access1 request not sent", result["person"], camera)
        return True

    def status(self) -> dict:
        with self._lock:
            now = self._clock()
            return {"camera": self._camera, "active": now < self._until,
                    "remaining_seconds": max(0, round(self._until - now)),
                    "started_at": self._started or None, "results": list(self._results)}


# ---------- apply ----------

def apply_setup(data_dir: Path, profiles, body: dict) -> dict:
    """Validate everything first, then write all affected files as one operation."""
    if not isinstance(body, dict) or set(body) - {"connection", "cameras", "settings"}:
        raise ValueError("unknown setup section")
    writes, restart = {}, False
    lock = getattr(profiles, "_lock", None) or threading.RLock()
    with _files_lock, lock:
        connection = body.get("connection")
        if connection is not None:
            if not isinstance(connection, dict) or set(connection) - set(CONNECTION_FIELDS):
                raise ValueError("unknown connection section")
            stored = _read_json(data_dir / CONNECTION_FILE, {}) or {}
            for section, values in connection.items():
                stored[section] = {**(stored.get(section) or {}), **_clean_connection(section, values)}
            writes[CONNECTION_FILE], restart = stored, True
        settings = body.get("settings")
        if settings is not None:
            if not isinstance(settings, dict) or set(settings) - set(SETTINGS_FIELDS):
                raise ValueError("unknown setting")
            overlay = _read_json(data_dir / SETTINGS_FILE, {}) or {}
            for key, value in settings.items():
                low, high = SETTINGS_FIELDS[key]
                if type(value) is not int or not low <= value <= high:
                    raise ValueError(f"{key} must be between {low} and {high}")
                overlay[key] = value
            writes[SETTINGS_FILE], restart = overlay, True
        cameras = body.get("cameras")
        if cameras is not None:
            from .camera_profiles import DECODE_KEYS, decode_override
            if not isinstance(cameras, dict):
                raise ValueError("invalid cameras")
            stored = _read_json(data_dir / PROFILES_FILE, {}) or {}
            for camera, decode in cameras.items():
                if not isinstance(camera, str) or not camera.strip() or len(camera) > 128:
                    raise ValueError("invalid camera name")
                entry = dict(stored.get(camera) or {})
                for key in DECODE_KEYS:
                    entry.pop(key, None)
                if decode is not None:
                    if not isinstance(decode, dict) or set(decode) != set(DECODE_KEYS) \
                            or decode_override(decode) is None:
                        raise ValueError(f"invalid stream decode for {camera}")
                    entry.update({key: decode[key] for key in DECODE_KEYS})
                stored[camera] = entry
            writes[PROFILES_FILE] = stored
        if not writes:
            raise ValueError("nothing to apply")
        setup = SetupState(data_dir).read()
        setup["last_apply"] = time.time()
        writes[SETUP_FILE] = setup
        transaction(data_dir, writes)
    return {"applied": sorted(name for name in writes if name != SETUP_FILE),
            "restart_required": restart}


# ---------- 6.9.3 site export / import ----------
#
# One secret-free file to reproduce a site's setup: FaceID settings (effective values) and each
# camera's profile with its stream decode. Never included: Frigate/MQTT connection, passwords,
# direct RTSP addresses, setup.key, geometry records or the gallery (that has its own backup).
# Import is previewed first and applied through the same journaled transaction as setup apply.

SITE_FORMAT = "faceid-site"
SITE_VERSION = 1
# Same ranges as the Settings tab and the setup apply (backup_dir is site-specific: excluded).
SITE_SETTINGS = {
    "match_threshold": (float, 0.2, 0.9), "unknown_threshold": (float, 0.1, 0.8),
    "match_margin": (float, 0.0, 0.5), "suggest_threshold": (float, 0.1, 0.9),
    "cluster_eps": (float, 0.3, 0.8), "ignore_threshold": (float, 0.1, 0.9),
    "ignore_margin": (float, 0.0, 0.5), "dedupe_threshold": (float, 0.5, 0.95),
    "max_faces_per_person": (int, 5, 100), "trimmed_keep": (int, 0, 100),
    "match_top_k": (int, 1, 10), "min_confirmations": (int, 1, 6),
    "hires_enroll": (bool,), "backup_enabled": (bool,), "backup_hour": (int, 0, 23),
    "backup_keep": (int, 1, 90), "stream_warm_seconds": (int, 0, 3600),
}
SITE_CAMERA_KEYS = (
    "enabled", "observation_source", "trust_local_video_network", "min_face_px", "night_min_face_px",
    "role", "mode", "burst_frames", "high_resolution", "fast_recognition", "recognition_mode",
    "recognition_interval_seconds", "recognition_rearm_seconds", "critical",
    "require_second_factor", "liveness_mode", "roi",
)


def _site_value(key, value):
    """Validated setting value, or ValueError."""
    kind = SITE_SETTINGS[key]
    if kind[0] is bool:
        if type(value) is not bool:
            raise ValueError(f"{key} must be true or false")
        return value
    if type(value) is bool or not isinstance(value, (int, float)):
        raise ValueError(f"{key} must be a number")
    if kind[0] is int and value != int(value):
        raise ValueError(f"{key} must be a whole number")
    value = kind[0](value)
    if not kind[1] <= value <= kind[2]:
        raise ValueError(f"{key} must be between {kind[1]} and {kind[2]}")
    return value


def _profiles_from(stored: dict, default_min_face_px: int, path: Path):
    from .camera_profiles import CameraProfiles
    path.write_text(json.dumps(stored, ensure_ascii=False), encoding="utf-8")
    return CameraProfiles(path, default_min_face_px)


def _camera_view(profile: dict) -> dict:
    view = {key: profile[key] for key in SITE_CAMERA_KEYS}
    view.update(profile["decode"] or {})
    return view


def export_site(cfg: dict, data_dir: Path, profiles, version: str) -> dict:
    from .camera_profiles import CameraProfiles
    faceid = cfg.get("faceid") or {}
    settings = {}
    for key in SITE_SETTINGS:
        if key in faceid:
            try:
                settings[key] = _site_value(key, faceid[key])
            except ValueError:
                pass
    profiles = profiles or CameraProfiles(data_dir / PROFILES_FILE)
    stored = _read_json(data_dir / PROFILES_FILE, {}) or {}
    cameras = {name: _camera_view(profiles.get(name))
               for name in sorted(stored) if isinstance(stored[name], dict)}
    return {"format": SITE_FORMAT, "version": SITE_VERSION, "faceid_version": version,
            "exported_at": round(time.time()), "settings": settings, "cameras": cameras}


def import_site(cfg: dict, data_dir: Path, profiles, site, frigate_names, *, apply=False) -> dict:
    """Preview (apply=False) or apply a site file. frigate_names None = Frigate unreachable."""
    import tempfile
    from .camera_profiles import DECODE_KEYS, decode_override
    if (not isinstance(site, dict) or site.get("format") != SITE_FORMAT
            or site.get("version") != SITE_VERSION):
        raise ValueError("not a FaceID site file")
    settings, cameras = site.get("settings") or {}, site.get("cameras") or {}
    if not isinstance(settings, dict) or not isinstance(cameras, dict):
        raise ValueError("invalid site file")
    faceid = cfg.get("faceid") or {}
    result = {"settings": {}, "cameras": {}, "skipped": [], "warnings": [], "ignored": []}
    for key, value in settings.items():
        if key not in SITE_SETTINGS:
            result["ignored"].append(key)
            continue
        value = _site_value(key, value)
        if faceid.get(key) != value:
            result["settings"][key] = {"from": faceid.get(key), "to": value}
    merged = {**faceid, **{key: change["to"] for key, change in result["settings"].items()}}
    if float(merged.get("unknown_threshold", 0.35)) >= float(merged.get("match_threshold", 0.5)):
        raise ValueError("unknown_threshold must be lower than match_threshold")
    lock = getattr(profiles, "_lock", None) or threading.RLock()
    default_px = getattr(profiles, "default_min_face_px", 48)
    with _files_lock, lock, tempfile.TemporaryDirectory() as work:
        work = Path(work)
        stored = _read_json(data_dir / PROFILES_FILE, {}) or {}
        candidate = _profiles_from(stored, default_px, work / "candidate.json")
        before = _profiles_from(stored, default_px, work / "before.json")
        before_views = {name: _camera_view(before.get(name)) for name in stored}
        for name, entry in cameras.items():
            if frigate_names is None or name not in frigate_names:
                result["skipped"].append(name)
                continue
            if not isinstance(entry, dict):
                raise ValueError(f"invalid camera {name}")
            args = {key: entry[key] for key in SITE_CAMERA_KEYS if key in entry}
            if (args.get("observation_source") == "direct_rtsp"
                    and not (stored.get(name) or {}).get("direct_rtsp_url")):
                args.pop("observation_source")
                result["warnings"].append({"camera": name, "reason": "direct_rtsp_address_needed"})
            try:
                candidate.update(name, **{"min_face_px": default_px, "role": "observation", **args})
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid camera {name}: {exc}") from None
            edited = _read_json(candidate.path, {}) or {}
            for key in DECODE_KEYS:
                edited[name].pop(key, None)
            decode = {key: entry[key] for key in DECODE_KEYS if key in entry}
            if decode:
                if set(decode) != set(DECODE_KEYS) or decode_override(decode) is None:
                    raise ValueError(f"invalid stream decode for {name}")
                edited[name].update(decode)
            candidate.path.write_text(json.dumps(edited, ensure_ascii=False), encoding="utf-8")
            after = _camera_view(candidate.get(name))
            old = before_views.get(name)
            changed = ["new"] if old is None else sorted(
                key for key in set(old) | set(after) if old.get(key) != after.get(key))
            if changed:
                result["cameras"][name] = changed
        final = _read_json(candidate.path, {}) or {}
        result["changes"] = bool(result["settings"] or result["cameras"])
        result["restart_required"] = bool(result["settings"])
        if not apply or not result["changes"]:
            result["applied"] = []
            return result
        writes = {}
        if result["settings"]:
            overlay = _read_json(data_dir / SETTINGS_FILE, {}) or {}
            overlay.update({key: change["to"] for key, change in result["settings"].items()})
            writes[SETTINGS_FILE] = overlay
        if result["cameras"]:
            # Only the changed cameras are replaced; every other entry stays byte-for-byte.
            writes[PROFILES_FILE] = {**stored, **{name: final[name] for name in result["cameras"]}}
        setup = SetupState(data_dir).read()
        setup["last_apply"] = time.time()
        writes[SETUP_FILE] = setup
        transaction(data_dir, writes)
    result["applied"] = sorted(name for name in writes if name != SETUP_FILE)
    return result


# ---------- effective configuration ----------

EFFECTIVE_KEYS = (
    "backend", "stream_warm_seconds", "stream_decode_threads", "stream_gpu_decode",
    "stream_decode_width", "frigate_box_association", "match_threshold", "unknown_threshold",
    "match_margin", "min_confirmations", "min_face_quality", "liveness_enabled",
    "liveness_threshold", "liveness_required_frames", "recognition_session_seconds",
    "access_control_enabled", "frigate_topic_prefix", "mqtt_prefix",
)
PROFILE_KEYS = ("enabled", "observation_source", "trust_local_video_network", "direct_rtsp_configured",
                "role", "mode", "min_face_px", "liveness_mode", "recognition_mode", "fast_recognition",
                "high_resolution", "roi")


def effective_config(cfg: dict, processor, engine=None, gallery=None) -> dict:
    """What FaceID is actually running with, without secrets (logged at startup)."""
    from . import live_frames
    from .camera_profiles import DECODE_KEYS
    faceid = cfg.get("faceid") or {}
    profiles = getattr(processor, "camera_profiles", None)
    geometry = getattr(processor, "setup_geometry", None)
    global_decode = (live_frames.GPU_DECODE, live_frames.DECODE_WIDTH, live_frames.DECODE_STEP)
    cameras = {}
    names = set(profiles.all()) if profiles else set()
    names |= set(getattr(processor, "cameras", set()) or set())
    for camera in sorted(names):
        profile = profiles.get(camera) if profiles else {}
        decode = live_frames.CAMERA_DECODE.get(camera)
        cameras[camera] = {**{key: profile.get(key) for key in PROFILE_KEYS},
                           "decode": dict(zip(DECODE_KEYS, decode or global_decode)),
                           "decode_source": "camera" if decode else "global",
                           "box_association": bool(getattr(processor, "box_association", False)) and (
                               geometry.box_association_allowed(camera) if geometry else True)}
    persons = gallery.persons() if gallery is not None else {}
    access1 = getattr(processor, "access1", None)
    try:
        access1_status = access1.status() if access1 is not None else None
    except Exception:
        access1_status = {"state": "unavailable"}
    return {
        "options": {key: faceid.get(key) for key in EFFECTIVE_KEYS},
        "connection": redacted_connection(cfg),
        "engine": {"active_backend": getattr(engine, "active_backend", None),
                   "native_device": getattr(engine, "native_device", None)},
        "reader": {"gpu_decode": live_frames.GPU_DECODE, "decode_width": live_frames.DECODE_WIDTH,
                   "decode_step": live_frames.DECODE_STEP,
                   "warm_seconds": getattr(getattr(getattr(processor, "frigate", None), "live_frames", None),
                                           "observation_linger", None)},
        "box_association": bool(getattr(processor, "box_association", False)),
        "liveness": (processor.liveness.status() if getattr(processor, "liveness", None)
                     and hasattr(processor.liveness, "status") else None),
        "cameras": cameras,
        "gallery": {"persons": len(persons),
                    "reference_photos": sum(int(p.get("count", 0)) for p in persons.values())},
        "access1": None if access1_status is None else {
            key: access1_status.get(key) for key in ("enabled", "state", "secure", "directory_running")},
        "setup_mode": list(getattr(processor, "setup_missing", []) or []),
    }
