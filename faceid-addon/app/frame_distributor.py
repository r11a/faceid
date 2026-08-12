"""Decode an event once and share bounded JPEG frames between recognition paths."""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import threading
import time
from pathlib import Path

import cv2
import numpy as np

log = logging.getLogger("faceid.frames")


class FrameDistributor:
    def __init__(self, data_dir: Path, media_store, *, decode_mode: str = "auto", max_frames: int = 24):
        self.media_store = media_store
        self.root = data_dir / "media_cache" / "frames"
        self.root.mkdir(parents=True, exist_ok=True)
        self.decode_mode = decode_mode if decode_mode in ("auto", "software", "vaapi", "cuda") else "auto"
        self.max_frames = max(4, min(int(max_frames), 120))
        self.max_cached_events = 25
        self._lock = threading.Lock()
        self._stats = {"requests": 0, "cache_hits": 0, "ffmpeg": 0, "opencv": 0,
                       "hardware": 0, "fallbacks": 0, "last_backend": None, "last_error": None}

    def _dir(self, event_id: str) -> Path:
        return self.root / self.media_store._path(event_id).stem

    def frames(self, event_id: str, *, limit: int | None = None) -> list[tuple[int, np.ndarray]]:
        limit = max(1, min(int(limit or self.max_frames), self.max_frames))
        with self._lock:
            self.prune()
            self._stats["requests"] += 1
            target = self._dir(event_id)
            cached = sorted(target.glob("*.jpg")) if target.exists() else []
            if not cached:
                clip = self.media_store.clip_path(event_id)
                if clip is None:
                    self._stats["last_error"] = "clip unavailable"
                    return []
                target.mkdir(parents=True, exist_ok=True)
                cached = self._decode(clip, target, limit)
            else:
                self._stats["cache_hits"] += 1
        rows = []
        for path in cached[:limit]:
            image = cv2.imread(str(path))
            if image is not None:
                try:
                    index = int(path.stem)
                except ValueError:
                    index = len(rows)
                rows.append((index, image))
        return rows

    def _decode(self, clip: Path, target: Path, limit: int) -> list[Path]:
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg:
            modes = [self.decode_mode]
            if self.decode_mode in ("auto", "vaapi", "cuda"):
                modes.append("software")
            for position, mode in enumerate(modes):
                for old in target.glob("*.jpg"):
                    old.unlink(missing_ok=True)
                command = [ffmpeg, "-hide_banner", "-loglevel", "error"]
                if mode == "auto":
                    command += ["-hwaccel", "auto"]
                elif mode == "vaapi":
                    command += ["-hwaccel", "vaapi", "-hwaccel_output_format", "vaapi"]
                elif mode == "cuda":
                    command += ["-hwaccel", "cuda"]
                command += ["-i", str(clip), "-vf", f"select='not(mod(n,12))',scale='min(1280,iw)':-2",
                            "-vsync", "vfr", "-frames:v", str(limit), "-q:v", "3", str(target / "%06d.jpg")]
                try:
                    result = subprocess.run(command, capture_output=True, timeout=120, check=False)
                    files = sorted(target.glob("*.jpg"))
                    if result.returncode == 0 and files:
                        self._stats["ffmpeg"] += 1
                        self._stats["hardware"] += int(mode != "software")
                        self._stats["fallbacks"] += int(position > 0)
                        self._stats["last_backend"] = f"ffmpeg-{mode}"
                        self._stats["last_error"] = None
                        self._write_manifest(target, mode, len(files))
                        return files
                    self._stats["last_error"] = result.stderr.decode("utf-8", "replace")[-300:]
                except (OSError, subprocess.TimeoutExpired) as exc:
                    self._stats["last_error"] = str(exc)[:300]
            self._stats["fallbacks"] += 1
        files = self._decode_opencv(clip, target, limit)
        self._stats["opencv"] += 1
        self._stats["last_backend"] = "opencv-software"
        return files

    def _decode_opencv(self, clip: Path, target: Path, limit: int) -> list[Path]:
        cap = cv2.VideoCapture(str(clip))
        try:
            total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            if total <= 0:
                return []
            indices = np.linspace(0, total - 1, min(limit, total), dtype=int)
            files = []
            for number, frame_index in enumerate(indices, 1):
                cap.set(cv2.CAP_PROP_POS_FRAMES, int(frame_index))
                ok, frame = cap.read()
                if not ok or frame is None:
                    continue
                path = target / f"{number:06d}.jpg"
                if cv2.imwrite(str(path), frame, [cv2.IMWRITE_JPEG_QUALITY, 88]):
                    files.append(path)
            self._write_manifest(target, "opencv-software", len(files))
            return files
        finally:
            cap.release()

    @staticmethod
    def _write_manifest(target: Path, backend: str, count: int):
        (target / "manifest.json").write_text(json.dumps(
            {"backend": backend, "frames": count, "created": time.time()}, indent=2
        ), "utf-8")

    def report(self) -> dict:
        return {**self._stats, "requested_mode": self.decode_mode,
                "ffmpeg_available": shutil.which("ffmpeg") is not None}

    def prune(self):
        cutoff = time.time() - float(getattr(self.media_store, "retention_seconds", 86400))
        directories = sorted(
            (path for path in self.root.iterdir() if path.is_dir()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for index, directory in enumerate(directories):
            if not directory.is_dir():
                continue
            try:
                if index < self.max_cached_events and directory.stat().st_mtime >= cutoff:
                    continue
                for path in directory.iterdir():
                    path.unlink(missing_ok=True)
                directory.rmdir()
            except OSError:
                continue
