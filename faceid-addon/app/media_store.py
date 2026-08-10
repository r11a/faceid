"""Bounded, shared Frigate media cache.

One event clip is downloaded once and then reused by the browser, clip analysis and
future evidence helpers.  Keeping the file on disk avoids retaining decoded frames in
RAM and lets Starlette serve byte ranges correctly through Home Assistant ingress.
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from pathlib import Path

log = logging.getLogger("faceid.media")


class EventMediaStore:
    def __init__(
        self, data_dir: Path, frigate, *, max_clip_bytes: int = 150_000_000,
        max_cache_bytes: int = 1_000_000_000, retention_hours: float = 24.0,
    ):
        self.frigate = frigate
        self.cache_dir = data_dir / "media_cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.max_clip_bytes = max(10_000_000, int(max_clip_bytes))
        self.max_cache_bytes = max(self.max_clip_bytes, int(max_cache_bytes))
        self.retention_seconds = max(3600.0, float(retention_hours) * 3600.0)
        self._locks_guard = threading.Lock()
        self._locks: dict[str, tuple[threading.Lock, int]] = {}
        self.prune()

    def _path(self, event_id: str) -> Path:
        digest = hashlib.sha256(event_id.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{digest}.mp4"

    def _lock_for(self, event_id: str) -> threading.Lock:
        with self._locks_guard:
            lock, users = self._locks.get(event_id, (threading.Lock(), 0))
            self._locks[event_id] = (lock, users + 1)
            return lock

    def _release_lock(self, event_id: str, lock: threading.Lock):
        with self._locks_guard:
            current = self._locks.get(event_id)
            if current is None or current[0] is not lock:
                return
            if current[1] <= 1:
                self._locks.pop(event_id, None)
            else:
                self._locks[event_id] = (lock, current[1] - 1)

    @staticmethod
    def _valid_mp4(path: Path) -> bool:
        try:
            if path.stat().st_size < 1024:
                return False
            with path.open("rb") as fh:
                return b"ftyp" in fh.read(64)
        except OSError:
            return False

    def clip_path(self, event_id: str, *, refresh: bool = False) -> Path | None:
        """Return a playable local MP4, downloading it at most once concurrently."""
        target = self._path(event_id)
        if not refresh and self._valid_mp4(target):
            try:
                os.utime(target, None)
            except OSError:
                pass
            return target
        lock = self._lock_for(event_id)
        try:
            with lock:
                if not refresh and self._valid_mp4(target):
                    return target
                temporary = target.with_suffix(
                    f".part-{os.getpid()}-{threading.get_ident()}"
                )
                temporary.unlink(missing_ok=True)
                try:
                    ok = self.frigate.download_clip(
                        event_id, str(temporary), max_bytes=self.max_clip_bytes,
                    )
                    if not ok or not self._valid_mp4(temporary):
                        temporary.unlink(missing_ok=True)
                        return None
                    os.replace(temporary, target)
                    self.prune(protect=target)
                    return target
                finally:
                    temporary.unlink(missing_ok=True)
        finally:
            self._release_lock(event_id, lock)

    def status(self, event_id: str) -> dict:
        path = self._path(event_id)
        cached = self._valid_mp4(path)
        metadata = self.frigate.event(event_id)
        return {
            "cached": cached,
            "has_clip": bool(metadata.get("has_clip")) if metadata else None,
            "has_recording_window": bool(
                metadata and metadata.get("camera") and metadata.get("start_time")
            ),
            "camera": metadata.get("camera") if metadata else None,
            "start_time": metadata.get("start_time") if metadata else None,
            "end_time": metadata.get("end_time") if metadata else None,
        }

    def prune(self, *, protect: Path | None = None):
        """Apply age and total-size limits without touching a file being returned."""
        now = time.time()
        files = []
        for path in self.cache_dir.glob("*.mp4"):
            try:
                stat = path.stat()
            except OSError:
                continue
            if path != protect and now - stat.st_mtime > self.retention_seconds:
                path.unlink(missing_ok=True)
                continue
            files.append((stat.st_mtime, stat.st_size, path))
        total = sum(size for _, size, _ in files)
        for _, size, path in sorted(files):
            if total <= self.max_cache_bytes:
                break
            if path == protect:
                continue
            path.unlink(missing_ok=True)
            total -= size

    def report(self) -> dict:
        files = list(self.cache_dir.glob("*.mp4"))
        size = 0
        for path in files:
            try:
                size += path.stat().st_size
            except OSError:
                pass
        return {
            "clips": len(files), "bytes": size,
            "limit_bytes": self.max_cache_bytes,
            "retention_hours": round(self.retention_seconds / 3600, 1),
        }
