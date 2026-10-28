"""Minimaler Frigate-HTTP-Client: Snapshot-Crops holen, sub_label setzen."""
import logging
import math
import re
import threading
import warnings
from urllib3.exceptions import InsecureRequestWarning
import time
from urllib.parse import quote, urlsplit

import cv2
import numpy as np
import requests
from .live_frames import LiveFramePool

log = logging.getLogger("faceid.frigate")

# Keep the warning visible once per message/location rather than on every
# snapshot request. This does not modify Session.verify or authentication.
warnings.filterwarnings("default", category=InsecureRequestWarning,
                        module=r"urllib3\.connectionpool")


def camera_frame_bytes(frigate, camera, profile, *, timeout=None):
    """One source choice for native sampling, its preview and its diagnostics."""
    if profile.get("recognition_mode", "event") != "event" and profile.get("high_resolution"):
        return frigate.main_stream_frame_bytes(camera)
    if timeout is None:
        return frigate.latest_frame_bytes(camera)
    return frigate.latest_frame_bytes(camera, timeout=timeout)


def sighting_recording_path(metadata: dict | None) -> str | None:
    """A bounded recording window for a local face sighting, never an event ID."""
    if not metadata or not metadata.get("camera"):
        return None
    try:
        start = float(metadata["start_ts"])
        now = time.time()
        end = float(metadata["end_ts"]) if metadata.get("end_ts") is not None else now
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end < start or start >= now:
            return None
        # Recognition can happen long after a track first appears. Keep that
        # evidence in the bounded clip, even for a track lasting hours.
        if metadata.get("recognition_ts") is not None:
            recognized = float(metadata["recognition_ts"])
            if not math.isfinite(recognized) or not start <= recognized <= min(end, now):
                return None
            start = max(start, recognized - 2.0)
            end = min(end, start + 60.0, now)
        else:
            end = min(end + 2.0, start + 62.0, now)
            start = max(0.0, start - 2.0)
        if end <= start:
            return None
        camera = quote(str(metadata["camera"]), safe="")
        return f"/api/{camera}/start/{start:.3f}/end/{end:.3f}/clip.mp4"
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


class FrigateAPI:
    supports_live_frames = True

    def __init__(
        self, base_url: str, timeout: float = 6.0, *,
        username: str = "", password: str = "", verify_tls: bool = True,
    ):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.username = username
        self.password = password
        self.verify_tls = verify_tls
        self.session = requests.Session()
        self.session.verify = verify_tls
        self._authenticated = False
        self._stream_config_lock = threading.Lock()
        self._stream_config_until = 0.0
        self._main_streams = {}
        self._main_captures = threading.BoundedSemaphore(2)
        self._main_pending_lock = threading.Lock()
        self._main_pending = {}
        self.live_frames = LiveFramePool()

    @property
    def secure_mode(self) -> bool:
        return self.base.startswith("https://") and ":5000" not in self.base

    def _login(self) -> bool:
        if not self.username or not self.password:
            return False
        try:
            response = self.session.post(
                f"{self.base}/api/login",
                json={"user": self.username, "password": self.password},
                timeout=self.timeout,
                verify=self.session.verify,
            )
            self._authenticated = response.status_code in (200, 202)
            if not self._authenticated:
                log.warning("Frigate login failed: HTTP %s", response.status_code)
            return self._authenticated
        except requests.RequestException as exc:
            log.warning("Frigate login failed: %s", exc)
            return False

    def request(self, method: str, path: str, **kwargs):
        """Authenticated Frigate request with one automatic session refresh."""
        if self.username and not self._authenticated:
            self._login()
        kwargs.setdefault("timeout", self.timeout)
        # Preserve the configured setting when Requests merges environment CA defaults.
        kwargs["verify"] = self.session.verify
        response = self.session.request(method, f"{self.base}{path}", **kwargs)
        if response.status_code == 401 and self.username and self._login():
            response.close()
            response = self.session.request(
                method, f"{self.base}{path}", **kwargs
            )
        return response

    def connection_status(self) -> dict:
        result = {
            "url": self.base,
            "credentials_configured": bool(self.username and self.password),
            "authenticated": False,
            "tls_verified": False,
            "tls_verification_enabled": bool(self.session.verify),
            "secure_mode": self.secure_mode,
            "reachable": False,
        }
        try:
            response = self.request("GET", "/api/profile", timeout=self.timeout)
            result["reachable"] = response.status_code == 200
            result["authenticated"] = bool(result["reachable"] and self._authenticated)
            result["tls_verified"] = bool(result["reachable"] and self.secure_mode and self.session.verify)
            result["http_status"] = response.status_code
            response.close()
        except requests.RequestException as exc:
            result["error"] = ("TLS certificate verification failed" if isinstance(exc, requests.exceptions.SSLError)
                               else "Frigate connection timed out" if isinstance(exc, requests.exceptions.Timeout)
                               else "Frigate connection failed")
        return result

    def snapshot(self, event_id: str, crop: bool = True) -> np.ndarray | None:
        """Aktuellen Person-Snapshot eines Events als BGR-Bild (crop=Person-Box)."""
        url = f"{self.base}/api/events/{event_id}/snapshot.jpg"
        try:
            r = self.request(
                "GET", f"/api/events/{event_id}/snapshot.jpg",
                params={"crop": int(crop), "quality": 100},
            )
            if r.status_code != 200 or not r.content:
                return None
            img = cv2.imdecode(np.frombuffer(r.content, np.uint8), cv2.IMREAD_COLOR)
            return img
        except requests.RequestException as e:
            log.warning("snapshot %s failed: %s", event_id, e)
            return None

    def live_event_frame(self, event_id: str) -> np.ndarray | None:
        """Read an active event's full detect frame, never an archived snapshot.

        The caller must supply and retain the MQTT snapshot.frame_time as a
        conservative capture-time bound. HTTP receipt time is NOT capture time.
        Frigate marks active responses no-store, completed snapshots cacheable.
        """
        started = time.monotonic()
        r = None
        reason = "unknown"
        try:
            r = self.request(
                "GET", f"/api/events/{quote(event_id, safe='')}/snapshot.jpg",
                params={"crop": 0, "timestamp": 0, "bbox": 0, "quality": 100},
                headers={"Cache-Control": "no-cache, no-store"}, timeout=2.0,
            )
            cache = {part.strip().lower() for part in r.headers.get("Cache-Control", "").split(",")}
            if r.status_code != 200:
                reason = f"http_{r.status_code}"
            elif "no-store" not in cache or r.headers.get("Age", "0") != "0":
                reason = "not_confirmed_active"
            elif not r.content or len(r.content) > 20_000_000:
                reason = "empty_or_oversized_image"
            else:
                img = cv2.imdecode(np.frombuffer(r.content, np.uint8), cv2.IMREAD_COLOR)
                if img is not None:
                    return img
                reason = "invalid_image"
        except requests.RequestException as exc:
            reason = type(exc).__name__
        finally:
            if r is not None:
                r.close()
        # Never log URLs, cookies, credentials or the response body.
        log.info("live event frame %s unavailable: %s elapsed=%.3fs",
                 event_id, reason, time.monotonic() - started)
        return None

    def latest_frame_bytes(self, camera: str, *, timeout: float | None = None) -> bytes | None:
        """Fetch Frigate's current camera frame without exposing its credentials."""
        try:
            encoded = quote(str(camera), safe="")
            r = self.request("GET", f"/api/{encoded}/latest.jpg",
                             timeout=self.timeout * 2 if timeout is None else timeout)
            if r.status_code != 200 or not r.content or len(r.content) > 20_000_000:
                return None
            return r.content
        except requests.RequestException as exc:
            log.debug("latest frame %s failed: %s", camera, exc)
            return None

    def _main_stream(self, camera):
        with self._stream_config_lock:
            if time.monotonic() >= self._stream_config_until:
                response = None
                self._main_streams = {}
                # Cache missing/unsupported config too, avoiding a request storm.
                self._stream_config_until = time.monotonic() + 60
                try:
                    response = self.request("GET", "/api/config", timeout=2)
                    if response.status_code != 200:
                        return None
                    config = response.json()
                    streams = (config.get("go2rtc") or {}).get("streams") or {}
                    for name, settings in (config.get("cameras") or {}).items():
                        configured = (settings.get("live") or {}).get("streams") or {}
                        choices = list(configured.values()) if isinstance(configured, dict) else []
                        # Frigate uses the camera-named restream if no live alias is configured.
                        if not choices:
                            choices = [name]
                        stream = next((value for value in choices if isinstance(value, str)
                                       and value in streams and re.fullmatch(r"[A-Za-z0-9_.-]+", value)
                                       and value not in (".", "..")), None)
                        if stream:
                            self._main_streams[name] = stream
                except (requests.RequestException, ValueError, TypeError, AttributeError):
                    return None
                finally:
                    if response is not None:
                        response.close()
            return self._main_streams.get(camera)

    def _live_url(self, camera):
        stream = self._main_stream(camera)
        host = urlsplit(self.base).hostname
        if not stream or not host:
            return None
        # Connect only to the configured Frigate host and known restream alias.
        # HTTP/camera credentials must never be repurposed as RTSP credentials.
        authority = f"[{host}]" if ":" in host else host
        return f"rtsp://{authority}:8554/{stream}"

    def sync_live_cameras(self, cameras):
        sources = {camera: url for camera in cameras if (url := self._live_url(camera))}
        self.live_frames.sync(sources)

    def main_stream_frame_bytes(self, camera: str) -> bytes | None:
        """Read the latest full frame; bounded authenticated capture is a fallback."""
        url = self._live_url(camera)
        if not url:
            return None
        reader = self.live_frames.ensure(camera, url)
        if reader is not None:
            content = reader.frame()
            if content:
                return content
            if reader.report()["state"] in ("connecting", "catching_up"):
                return None
        with self._main_pending_lock:
            flight = self._main_pending.get(camera)
            owner = flight is None
            if owner:
                flight = {"ready": threading.Event(), "content": None}
                self._main_pending[camera] = flight
        if not owner:
            # Preview/analysis consume the same fresh frame as an in-flight scan.
            return flight["content"] if flight["ready"].wait(14) else None
        try:
            flight["content"] = self._capture_main_frame(camera)
            return flight["content"]
        finally:
            with self._main_pending_lock:
                self._main_pending.pop(camera, None)
                flight["ready"].set()

    def _capture_main_frame(self, camera):
        # Bounded waiting lets a preview of another camera use the next slot.
        # Native sampling itself still has at most two jobs, with no frame queue.
        if not self._main_captures.acquire(timeout=6):
            return None
        response = None
        try:
            stream = self._main_stream(camera)
            if not stream:
                return None
            response = self.request("GET", "/api/ffprobe/snapshot", params={
                "url": f"rtsp://127.0.0.1:8554/{stream}", "timeout": 5,
            }, timeout=6)
            if response.status_code != 200 or not response.content or len(response.content) > 20_000_000:
                return None
            return response.content
        except requests.RequestException:
            return None
        finally:
            if response is not None:
                response.close()
            self._main_captures.release()

    def cameras(self) -> list[str]:
        try:
            r = self.request("GET", "/api/config", timeout=self.timeout * 2)
            if r.status_code != 200:
                return []
            return sorted((r.json().get("cameras") or {}).keys())
        except (requests.RequestException, ValueError):
            return []

    def event(self, event_id: str) -> dict | None:
        """Fetch canonical Frigate metadata used for media fallbacks and diagnostics."""
        try:
            r = self.request("GET", f"/api/events/{quote(event_id, safe='')}")
            if r.status_code != 200:
                return None
            value = r.json()
            return value if isinstance(value, dict) else None
        except (requests.RequestException, ValueError) as exc:
            log.debug("event metadata %s failed: %s", event_id, exc)
            return None

    def recording_frame(self, camera: str, ts: float, *, timeout: float | None = None) -> np.ndarray | None:
        """Frame aus der AUFNAHME holen (volle Kamera-Auflösung statt Detect-Stream).
        Deutlich schärfere Gesichter, dafür langsamer — nur fürs Enrollment gedacht."""
        url = f"{self.base}/api/{camera}/recordings/{ts}/snapshot.jpg"
        try:
            r = self.request(
                "GET", f"/api/{camera}/recordings/{ts}/snapshot.jpg",
                timeout=self.timeout * 4 if timeout is None else timeout,
            )
            if r.status_code != 200 or not r.content:
                return None
            return cv2.imdecode(np.frombuffer(r.content, np.uint8), cv2.IMREAD_COLOR)
        except requests.RequestException as e:
            log.debug("recording frame %s@%s failed: %s", camera, ts, e)
            return None

    def download_clip(self, event_id: str, dest: str, max_bytes: int = 80_000_000,
                      *, metadata: dict | None = None) -> bool:
        """Ereignis-Clip (volle Aufnahme-Auflösung) nach ``dest`` streamen.

        Nur fürs Enrollment: ein Download deckt das ganze Ereignis ab, statt einzelne
        Zeitpunkte zu raten. ``max_bytes`` bricht überlange Clips ab.
        """
        encoded_id = quote(event_id, safe="")
        native = event_id.startswith("faceid-")
        if native:
            path = sighting_recording_path(metadata)
            if path is None:
                return False
            paths = [path]
        else:
            paths = [f"/api/events/{encoded_id}/clip.mp4"]
            metadata = None
        last_status = None
        for index, path in enumerate(paths):
            try:
                with self.request(
                    "GET", path, timeout=self.timeout * 12, stream=True,
                ) as r:
                    last_status = r.status_code
                    if r.status_code == 200:
                        written = 0
                        with open(dest, "wb") as fh:
                            for chunk in r.iter_content(chunk_size=1 << 18):
                                if not chunk:
                                    continue
                                written += len(chunk)
                                if written > max_bytes:
                                    log.warning("clip %s aborted (> %d bytes)", event_id, max_bytes)
                                    return False
                                fh.write(chunk)
                        if written > 1000:
                            return True
            except (requests.RequestException, OSError) as exc:
                log.debug("clip %s path %s failed: %s", event_id, path, type(exc).__name__)

            # An event clip may be absent even while the underlying recordings still
            # exist. Frigate officially exposes a camera/time recording endpoint.
            if index == 0 and not native:
                metadata = self.event(event_id)
                if metadata and metadata.get("camera") and metadata.get("start_time"):
                    start = max(0.0, float(metadata["start_time"]) - 2.0)
                    end = float(metadata.get("end_time") or metadata["start_time"] + 12.0) + 2.0
                    camera = quote(str(metadata["camera"]), safe="")
                    paths.append(f"/api/{camera}/start/{start:.3f}/end/{end:.3f}/clip.mp4")
        log.info(
            "clip unavailable for %s (event has_clip=%s, last HTTP=%s)",
            event_id, metadata.get("has_clip") if metadata else "unknown", last_status,
        )
        return False

    def set_sub_label(self, event_id: str, label: str, score: float):
        try:
            r = self.request(
                "POST", f"/api/events/{event_id}/sub_label",
                json={"subLabel": label[:100], "subLabelScore": round(score, 3)},
            )
            if r.status_code not in (200, 202):
                log.warning("sub_label %s -> %s: HTTP %s %s", event_id, label, r.status_code, r.text[:200])
        except requests.RequestException as e:
            log.warning("sub_label %s failed: %s", event_id, e)
