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
        self._busy_live = False
        self._last_live_activity = time.monotonic()
        self._local = threading.local()

    def __enter__(self):
        background = getattr(self._local, "background", False)
        with self._condition:
            if not background:
                self._live_waiters += 1
                self._last_live_activity = time.monotonic()
                self._condition.notify_all()
            try:
                deadline = getattr(self._local, "deadline", None) if background else None
                timeout = max(0, deadline-time.monotonic()) if deadline is not None else None
                admitted = self._condition.wait_for(lambda: not self._busy and (not background or (self._live_waiters == 0 and self._live_reservations == 0)), timeout=timeout)
                if not admitted:
                    raise TimeoutError("background_inference_busy")
                self._busy = True
                self._busy_live = not background
            finally:
                if not background:
                    self._live_waiters -= 1
        return self

    def __exit__(self, *args):
        with self._condition:
            if self._busy_live:
                self._last_live_activity = time.monotonic()
            self._busy_live = False
            self._busy = False
            self._condition.notify_all()

    @contextmanager
    def live_work(self):
        # Reserve priority across the gaps between a live frame's model calls.
        # This does not hold the inference mutex during HTTP or audit I/O.
        with self._condition:
            self._live_reservations += 1
            self._last_live_activity = time.monotonic()
        try:
            yield
        finally:
            with self._condition:
                self._live_reservations -= 1
                self._last_live_activity = time.monotonic()
                self._condition.notify_all()

    def live_activity(self):
        """Foreground demand only; background indexing cannot keep itself busy."""
        with self._condition:
            return {"active": bool(self._busy_live or self._live_waiters or self._live_reservations),
                    "last_activity_monotonic": self._last_live_activity}

    @contextmanager
    def background_work(self, *, timeout=None):
        previous = getattr(self._local, "background", False)
        previous_deadline = getattr(self._local, "deadline", None)
        self._local.background = True
        self._local.deadline = time.monotonic()+timeout if timeout is not None else previous_deadline
        try:
            yield
        finally:
            self._local.background = previous
            self._local.deadline = previous_deadline


class FaceEngine:
    supports_embedding_timing = True
    def __init__(self, det_size: int = 640, providers=None, backend: str = "auto",
                 cpu_threads: int = 0, cpu_allow_spinning: bool = True, native_cache_dir=None):
        from insightface.app import FaceAnalysis
        import onnxruntime as ort

        available = ort.get_available_providers()
        from .native_openvino import NATIVE_BACKENDS
        native_devices = NATIVE_BACKENDS.get(backend) if providers is None else None
        providers = providers or self.providers_for_backend("cpu" if native_devices else backend, available)
        self.backend_generation = 0
        self.native_device = None
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
        self._embedding_wait_ms = deque(maxlen=256)
        self._embedding_compute_ms = deque(maxlen=256)
        self._embedding_failures = 0
        self._embedding_calls = 0
        self._detection_wait_ms = deque(maxlen=256)
        self._detection_compute_ms = deque(maxlen=256)
        self._detection_calls = 0
        self._detection_failures = 0
        self._stream_frame_requests = 0
        self._stream_frame_unavailable = 0
        self._stream_frame_rejected = 0
        self.embedding_warmup = {"state": "not_started", "elapsed_ms": None}
        if native_devices:
            self._native_originals = {task: model.session for task, model in self.app.models.items()}
            detector = self.app.models["detection"]
            if hasattr(detector, "static_shape_sessions"):
                self._native_detector_static_shapes = detector.static_shape_sessions
            self._native_cache_dir = native_cache_dir or "/data/model-cache/openvino"
            self._select_native(native_devices)
        if self.native_device is None:
            self._prepare_embedding()

    def _select_native(self, devices):
        from .native_openvino import PROVIDER, prepare_sessions

        for device in devices:
            try:
                sessions, setup_ms, warm_ms = prepare_sessions(
                    self.app.models, device, self.det_size, self._native_cache_dir,
                    cpu_threads=self.cpu_threads)
            except Exception as error:
                log.warning("Native OpenVINO %s startup failed (%s)", device, type(error).__name__)
                continue
            self.native_device = sessions["detection"].device
            self.providers = [PROVIDER]
            if hasattr(self, "_native_detector_static_shapes"):
                # SCRFD would otherwise derive an ORT session for the input
                # resolution. The native session is already compiled for it.
                self.app.models["detection"].static_shape_sessions = False
            for task, session in sessions.items():
                self.app.models[task].session = session
                self.models[task]["providers"] = [PROVIDER]
                session.on_failure = self._native_failed
            self.embedding_warmup = {"state": "ready", "elapsed_ms": warm_ms}
            log.info("Native OpenVINO selected device=%s setup_ms=%s warmup_ms=%s",
                     self.native_device, setup_ms, warm_ms)
            return
        log.warning("Using ONNX Runtime CPU after native OpenVINO setup failed")

    def _native_failed(self):
        # Called under the inference scheduler. Never retry the failed call.
        failed_device = self.native_device
        self.backend_generation += 1
        self.native_device = None
        self.providers = ["CPUExecutionProvider"]
        if hasattr(self, "_native_detector_static_shapes"):
            self.app.models["detection"].static_shape_sessions = self._native_detector_static_shapes
        for task, session in self._native_originals.items():
            self.app.models[task].session = session
            self.models[task]["providers"] = session.get_providers()
        log.error("OpenVINO device=%s failed; invalidated recognition evidence", failed_device)
        if failed_device and failed_device.startswith("GPU"):
            self._select_native(("CPU",))
        self.backend_generation += 1
        log.info("Recognition fallback device=%s generation=%s",
                 self.native_device or "ONNX Runtime CPU", self.backend_generation)

    def _prepare_embedding(self):
        """One CPU inference before event processing; never creates face evidence."""
        if self.providers != ["CPUExecutionProvider"]:
            self.embedding_warmup = {"state": "skipped_accelerated", "elapsed_ms": None}
            return
        model = self.app.models.get("recognition")
        size = getattr(model, "input_size", None)
        if (not callable(getattr(model, "get_feat", None)) or
                not isinstance(size, (list, tuple)) or len(size) != 2 or
                any(type(value) is not int or not 1 <= value <= 512 for value in size)):
            self.embedding_warmup = {"state": "unsupported", "elapsed_ms": None}
            return
        started = time.perf_counter()
        state = "ready"
        try:
            with self._lock:
                # Same installed model/preprocessing. This local synthetic array
                # is not attached to any event, gallery, identity or liveness state.
                output = np.asarray(model.get_feat(np.zeros((size[1], size[0], 3), dtype=np.uint8)))
                if output.size == 0 or not np.all(np.isfinite(output)):
                    raise ValueError("invalid warmup output")
        except Exception:
            state = "failed"
            log.warning("embedding startup warmup failed; normal inference checks remain required")
        elapsed_ms = (time.perf_counter() - started) * 1000
        self.embedding_warmup = {"state": state, "elapsed_ms": round(elapsed_ms, 3)}
        log.info("embedding startup warmup state=%s elapsed_ms=%.1f", state, elapsed_ms)

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

    @property
    def active_backend(self):
        if self.native_device:
            return "openvino_gpu" if self.native_device.startswith("GPU") else "openvino_cpu"
        return "cpu" if self.providers == ["CPUExecutionProvider"] else self.backend

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
            "active_backend": self.active_backend,
            "native_device": self.native_device,
            "backend_generation": self.backend_generation,
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
            "embedding": self._embedding_health(),
            "stream_detection": self._detection_health(),
        }

    def _detection_health(self):
        with self._metrics_lock:
            waits = list(self._detection_wait_ms)
            computes = list(self._detection_compute_ms)
            result = {"calls": self._detection_calls, "failures": self._detection_failures,
                      "frame_requests": self._stream_frame_requests,
                      "unavailable_frames": self._stream_frame_unavailable,
                      "rejected_frames": self._stream_frame_rejected,
                      "samples": len(computes)}
        for name, samples in (("scheduler_wait", waits), ("compute", computes)):
            result[name] = {
                "p50_ms": round(float(np.percentile(samples, 50)), 3) if samples else None,
                "p95_ms": round(float(np.percentile(samples, 95)), 3) if samples else None,
            }
        return result

    def _embedding_health(self):
        with self._metrics_lock:
            waits = list(self._embedding_wait_ms)
            computes = list(self._embedding_compute_ms)
            result = {"calls": self._embedding_calls, "failures": self._embedding_failures,
                      "samples": len(computes), "warmup": dict(self.embedding_warmup)}
        for name, samples in (("scheduler_wait", waits), ("compute", computes)):
            result[name] = {
                "p50_ms": round(float(np.percentile(samples, 50)), 3) if samples else None,
                "p95_ms": round(float(np.percentile(samples, 95)), 3) if samples else None,
            }
        return result

    def live_work(self):
        return self._lock.live_work()

    def live_activity(self):
        return self._lock.live_activity()

    def model_work(self):
        # Liveness shares the same scheduler as detection/embedding.
        return self._lock

    def detect_frame_record(self, reader, *, admit=None):
        """Admit latest distinct input after scheduling, before costly detection.

        Local arrival age applies when processing starts, not after inference.
        The caller still validates generation, event and evidence expiry before
        accepting/submitting results. Rejection never masquerades as no face.
        """
        queued = time.perf_counter()
        with self._lock:
            scheduled = time.perf_counter()
            record = reader.frame_record()
            with self._metrics_lock:
                self._stream_frame_requests += 1
            started = time.monotonic()
            if record is None or not 0 <= started-record['arrived'] <= 1:
                with self._metrics_lock:
                    self._stream_frame_unavailable += 1
                return None, []
            record['processing_started_monotonic'] = started
            record['processing_age_ms'] = (started-record['arrived'])*1000
            record['detection_scheduler_wait_ms'] = (scheduled-queued)*1000
            if admit is not None and not admit(record):
                record['processing_rejected'] = 'duplicate_frame'
                with self._metrics_lock:
                    self._stream_frame_rejected += 1
                return record, []
            compute_started = time.perf_counter()
            failed = False
            try:
                return record, self._detect_faces(record['image'])
            except Exception:
                failed = True
                raise
            finally:
                compute_ms = (time.perf_counter()-compute_started)*1000
                record['detection_compute_ms'] = compute_ms
                with self._metrics_lock:
                    self._detection_calls += 1
                    self._detection_failures += int(failed)
                    self._detection_wait_ms.append((scheduled-queued)*1000)
                    self._detection_compute_ms.append(compute_ms)

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

    def embed_face(self, bgr: np.ndarray, face, *, timings=None):
        if timings is not None:
            timings.clear()
        if getattr(face, "embedding", None) is not None:
            return face
        queued = time.perf_counter()
        with self._lock:
            started = time.perf_counter()
            wait = started - queued
            if timings is not None:
                timings["embedding_scheduler_wait"] = wait
            # A concurrent caller may have populated this exact face object
            # while we waited. Do not calculate the same embedding twice.
            if getattr(face, "embedding", None) is not None:
                return face
            failed = False
            try:
                self.app.models["recognition"].get(bgr, face)
            except Exception:
                failed = True
                raise
            finally:
                compute = time.perf_counter() - started
                if timings is not None:
                    timings["embedding_compute"] = compute
                with self._metrics_lock:
                    self._embedding_calls += 1
                    self._embedding_failures += int(failed)
                    self._embedding_wait_ms.append(wait * 1000)
                    self._embedding_compute_ms.append(compute * 1000)
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

    def background_work(self, *, timeout=None):
        return self._lock.background_work(timeout=timeout)

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
