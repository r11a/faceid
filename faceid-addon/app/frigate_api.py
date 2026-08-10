"""Minimaler Frigate-HTTP-Client: Snapshot-Crops holen, sub_label setzen."""
import logging
from urllib.parse import quote

import cv2
import numpy as np
import requests

log = logging.getLogger("faceid.frigate")


class FrigateAPI:
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
            "authenticated": bool(self.username),
            "tls_verified": bool(self.verify_tls),
            "secure_mode": self.secure_mode,
            "reachable": False,
        }
        try:
            response = self.request("GET", "/api/profile", timeout=self.timeout)
            result["reachable"] = response.status_code == 200
            result["http_status"] = response.status_code
            response.close()
        except requests.RequestException as exc:
            result["error"] = str(exc)[:160]
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

    def latest_frame_bytes(self, camera: str) -> bytes | None:
        """Fetch Frigate's current camera frame without exposing its credentials."""
        try:
            encoded = quote(str(camera), safe="")
            r = self.request("GET", f"/api/{encoded}/latest.jpg", timeout=self.timeout * 2)
            if r.status_code != 200 or not r.content or len(r.content) > 20_000_000:
                return None
            return r.content
        except requests.RequestException as exc:
            log.debug("latest frame %s failed: %s", camera, exc)
            return None

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

    def recording_frame(self, camera: str, ts: float) -> np.ndarray | None:
        """Frame aus der AUFNAHME holen (volle Kamera-Auflösung statt Detect-Stream).
        Deutlich schärfere Gesichter, dafür langsamer — nur fürs Enrollment gedacht."""
        url = f"{self.base}/api/{camera}/recordings/{ts}/snapshot.jpg"
        try:
            r = self.request(
                "GET", f"/api/{camera}/recordings/{ts}/snapshot.jpg",
                timeout=self.timeout * 4,
            )
            if r.status_code != 200 or not r.content:
                return None
            return cv2.imdecode(np.frombuffer(r.content, np.uint8), cv2.IMREAD_COLOR)
        except requests.RequestException as e:
            log.debug("recording frame %s@%s failed: %s", camera, ts, e)
            return None

    def download_clip(self, event_id: str, dest: str, max_bytes: int = 80_000_000) -> bool:
        """Ereignis-Clip (volle Aufnahme-Auflösung) nach ``dest`` streamen.

        Nur fürs Enrollment: ein Download deckt das ganze Ereignis ab, statt einzelne
        Zeitpunkte zu raten. ``max_bytes`` bricht überlange Clips ab.
        """
        encoded_id = quote(event_id, safe="")
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
                log.debug("clip %s path %s failed: %s", event_id, path, exc)

            # An event clip may be absent even while the underlying recordings still
            # exist. Frigate officially exposes a camera/time recording endpoint.
            if index == 0:
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
