"""InsightFace-Wrapper: SCRFD-Detection + ArcFace-Embeddings (buffalo_l — dieselben Modelle wie Immich)."""
import threading
import logging
import time
from contextlib import contextmanager
from collections import deque
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import cv2
import numpy as np

log = logging.getLogger("faceid.engine")


class InferenceLock:
    """Serialize the model while letting queued live frames precede clip work."""
    def __init__(self):
        self._condition = threading.Condition()
        self._busy = False
        self._live_waiters = 0
        self._live_reservations = 0
        self._local = threading.local()

    def __enter__(self):
        background = getattr(self._local, "background", False)
        with self._condition:
            if not background:
                self._live_waiters += 1
                self._condition.notify_all()
            try:
                self._condition.wait_for(lambda: not self._busy and (not background or (self._live_waiters == 0 and self._live_reservations == 0)))
                self._busy = True
            finally:
                if not background:
                    self._live_waiters -= 1
        return self

    def __exit__(self, *args):
        with self._condition:
            self._busy = False
            self._condition.notify_all()

    @contextmanager
    def live_work(self):
        # Reserve priority across the gaps between a live frame's model calls.
        # This does not hold the inference mutex during HTTP or audit I/O.
        with self._condition:
            self._live_reservations += 1
        try:
            yield
        finally:
            with self._condition:
                self._live_reservations -= 1
                self._condition.notify_all()

    @contextmanager
    def background_work(self):
        previous = getattr(self._local, "background", False)
        self._local.background = True
        try:
            yield
        finally:
            self._local.background = previous


class FaceEngine:
    def __init__(self, det_size: int = 640, providers=None, backend: str = "auto",
                 cpu_threads: int = 0, cpu_allow_spinning: bool = True):
        from insightface.app import FaceAnalysis
        import onnxruntime as ort

        available = ort.get_available_providers()
        providers = providers or self.providers_for_backend(backend, available)
        self.backend = backend
        self.providers = providers
        self.available_providers = available

        if type(cpu_threads) is not int or not 0 <= cpu_threads <= 16:
            raise ValueError("cpu_threads must be an integer from 0 to 16")
        if type(cpu_allow_spinning) is not bool:
            raise ValueError("cpu_allow_spinning must be boolean")
        self.cpu_threads = cpu_threads
        self.cpu_allow_spinning = cpu_allow_spinning
        session_kwargs = {}
        self.cpu_tuning_applied = providers == ["CPUExecutionProvider"] and (cpu_threads != 0 or not cpu_allow_spinning)
        if self.cpu_tuning_applied:
            options = ort.SessionOptions()
            options.intra_op_num_threads = cpu_threads
            options.add_session_config_entry("session.intra_op.allow_spinning", "1" if cpu_allow_spinning else "0")
            options.add_session_config_entry("session.inter_op.allow_spinning", "1" if cpu_allow_spinning else "0")
            session_kwargs["sess_options"] = options
        log.info("InsightFace CPU tuning threads=%s allow_spinning=%s applied=%s",
                 cpu_threads, cpu_allow_spinning, self.cpu_tuning_applied)
        started = time.perf_counter()
        self.app = FaceAnalysis(
            name="buffalo_l",
            providers=providers,
            allowed_modules=["detection", "recognition"],
            addons=[],
            **session_kwargs,
        )
        self.app.prepare(ctx_id=0, det_size=(det_size, det_size))
        self.startup_seconds = time.perf_counter() - started
        self.det_size = det_size
        self.runtime = {}
        for package in ("insightface", "onnxruntime"):
            try:
                self.runtime[package] = version(package)
            except PackageNotFoundError:
                self.runtime[package] = None
        self.models = {
            task: {
                "file": Path(model.model_file).name,
                "providers": model.session.get_providers(),
            }
            for task, model in self.app.models.items()
        }
        log.info(
            "recognition backend %s, providers=%s, available=%s, startup=%.2fs",
            backend, providers, available, self.startup_seconds,
        )
        # onnxruntime-Sessions sind nicht garantiert threadsafe bei parallelem run()
        self._lock = InferenceLock()
        self._metrics_lock = threading.Lock()
        self._inference_ms = deque(maxlen=256)
        self._inference_calls = 0
        self._inference_failures = 0
    @staticmethod
    def providers_for_backend(backend: str, available: list[str]):
        backend = str(backend or "auto").lower()
        choices = {
            "cpu": ["CPUExecutionProvider"],
            "cuda": ["CUDAExecutionProvider", "CPUExecutionProvider"],
            "openvino": ["OpenVINOExecutionProvider", "CPUExecutionProvider"],
        }
        if backend == "auto":
            for name in ("CUDAExecutionProvider", "OpenVINOExecutionProvider"):
                if name in available:
                    return [name, "CPUExecutionProvider"]
            return ["CPUExecutionProvider"]
        requested = choices.get(backend)
        if requested is None:
            raise ValueError("backend must be auto, cpu, cuda or openvino")
        if requested[0] not in available:
            raise RuntimeError(
                f"requested backend {backend} is unavailable; providers: {available}"
            )
        return requested

    def health(self):
        with self._metrics_lock:
            samples = list(self._inference_ms)
            inference = {
                "calls": self._inference_calls,
                "failures": self._inference_failures,
                "samples": len(samples),
            }
        inference.update({
            "p50_ms": round(float(np.percentile(samples, 50)), 3) if samples else None,
            "p95_ms": round(float(np.percentile(samples, 95)), 3) if samples else None,
        })
        return {
            "backend": self.backend,
            "cpu_tuning": {"threads": self.cpu_threads, "allow_spinning": self.cpu_allow_spinning,
                           "applied": self.cpu_tuning_applied},
            "providers": self.providers,
            "available_providers": self.available_providers,
            "startup_seconds": round(self.startup_seconds, 3),
            "model_pack": "buffalo_l",
            "det_size": self.det_size,
            "runtime": dict(self.runtime),
            "models": {task: {**model, "providers": list(model["providers"])}
                       for task, model in self.models.items()},
            "inference": inference,
        }

    def live_work(self):
        return self._lock.live_work()

    def model_work(self):
        # Liveness shares the same scheduler as detection/embedding.
        return self._lock

    def detect_faces(self, bgr: np.ndarray):
        return self.faces(bgr, recognize=False)

    def _detect_faces(self, bgr: np.ndarray):
        # Same detector invocation and Face construction as installed
        # FaceAnalysis.get; only recognition is deferred for live access.
        from insightface.app.common import Face
        bboxes, kpss = self.app.det_model.detect(bgr, max_num=0, metric="default")
        return [Face(bbox=bboxes[i, 0:4], det_score=bboxes[i, 4],
                     kps=None if kpss is None else kpss[i])
                for i in range(bboxes.shape[0])]

    def embed_face(self, bgr: np.ndarray, face):
        if getattr(face, "embedding", None) is not None:
            return face
        with self._lock:
            self.app.models["recognition"].get(bgr, face)
        return face

    def faces(self, bgr: np.ndarray, *, recognize: bool = True):
        with self._lock:
            # Measure inference only; waiting for another caller is excluded.
            started = time.perf_counter()
            failed = False
            try:
                return self.app.get(bgr) if recognize else self._detect_faces(bgr)
            except Exception:
                failed = True
                raise
            finally:
                elapsed_ms = (time.perf_counter() - started) * 1000
                with self._metrics_lock:
                    self._inference_calls += 1
                    self._inference_failures += int(failed)
                    self._inference_ms.append(elapsed_ms)

    def background_work(self):
        return self._lock.background_work()

    @staticmethod
    def best_face(faces, min_px: int = 48, min_det: float = 0.55):
        """Größtes Gesicht, das Mindestgröße und Detection-Score erfüllt."""
        best, best_area = None, 0
        for f in faces:
            w = f.bbox[2] - f.bbox[0]
            h = f.bbox[3] - f.bbox[1]
            if w < min_px or h < min_px or f.det_score < min_det:
                continue
            if w * h > best_area:
                best, best_area = f, w * h
        return best


def find_face_padded(engine: "FaceEngine", bgr: np.ndarray, min_px: int = 60):
    """Detection mit Fallback für formatfüllende Porträts (SCRFD übersieht extreme
    Close-ups) — bei Bedarf mit Rand gepolstert erneut suchen.
    -> (face, bild) — bild ist ggf. die gepolsterte Variante, zu der die bbox passt."""
    face = FaceEngine.best_face(engine.faces(bgr), min_px=min_px)
    if face is not None:
        return face, bgr
    pad = int(0.3 * max(bgr.shape[:2]))
    padded = cv2.copyMakeBorder(bgr, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=(40, 40, 40))
    face = FaceEngine.best_face(engine.faces(padded), min_px=min_px)
    return face, padded


def crop_face(bgr: np.ndarray, bbox, margin: float = 0.35) -> np.ndarray:
    x1, y1, x2, y2 = [int(v) for v in bbox]
    mx, my = int((x2 - x1) * margin), int((y2 - y1) * margin)
    h, w = bgr.shape[:2]
    return bgr[max(0, y1 - my) : min(h, y2 + my), max(0, x1 - mx) : min(w, x2 + mx)]
