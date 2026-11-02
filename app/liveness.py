"""Local RGB presentation-attack detection for print and screen spoofs."""
from __future__ import annotations

import threading
import math
from collections import deque
from pathlib import Path

import cv2
import numpy as np


class LivenessDetector:
    """MiniFAS ONNX inference with conservative multi-frame consensus.

    RGB presentation-attack detection reduces print/screen attacks but cannot
    provide the guarantees of a depth/IR sensor. Required mode therefore blocks
    on errors rather than silently treating an unavailable model as live.
    """

    def __init__(
        self, *, model_path: str = "/opt/faceid/models/liveness.onnx",
        enabled: bool = True, threshold: float = 0.5, required_frames: int = 3,
        cpu_threads: int = 0, cpu_allow_spinning: bool = True,
    ):
        if type(cpu_threads) is not int or not 0 <= cpu_threads <= 16:
            raise ValueError("cpu_threads must be an integer from 0 to 16")
        if type(cpu_allow_spinning) is not bool:
            raise ValueError("cpu_allow_spinning must be boolean")
        self.cpu_threads = cpu_threads
        self.cpu_allow_spinning = cpu_allow_spinning
        self.model_path = Path(model_path)
        self.enabled = bool(enabled)
        self.threshold = float(threshold)
        self.required_frames = max(2, min(int(required_frames), 8))
        self._session = None
        self._lock = threading.RLock()
        self.last_error = None

    def _runtime(self):
        with self._lock:
            if self._session is not None:
                return self._session
            if not self.model_path.is_file():
                raise RuntimeError("liveness model is not installed")
            import onnxruntime as ort
            available = ort.get_available_providers()
            providers = [name for name in (
                "CUDAExecutionProvider", "OpenVINOExecutionProvider",
                "CPUExecutionProvider",
            ) if name in available]
            session_kwargs = {}
            if providers == ["CPUExecutionProvider"] and (self.cpu_threads or not self.cpu_allow_spinning):
                options = ort.SessionOptions()
                if self.cpu_threads:
                    options.intra_op_num_threads = self.cpu_threads
                for key in ("session.intra_op.allow_spinning", "session.inter_op.allow_spinning"):
                    options.add_session_config_entry(key, "1" if self.cpu_allow_spinning else "0")
                session_kwargs["sess_options"] = options
            self._session = ort.InferenceSession(
                str(self.model_path), providers=providers, **session_kwargs,
            )
            return self._session

    def prepare(self):
        """Load once before MQTT starts; errors still fail closed in analyze()."""
        if not self.enabled:
            return False
        try:
            self._runtime()
            return True
        except Exception as exc:
            self.last_error = str(exc)[:200]
            return False

    @staticmethod
    def _crop(image: np.ndarray, bbox, expansion: float = 1.5):
        height, width = image.shape[:2]
        x1, y1, x2, y2 = (float(value) for value in bbox)
        face_size = int(min(x2 - x1, y2 - y1))
        if face_size < 64:
            return None, face_size
        size = max(1, int(max(x2 - x1, y2 - y1) * expansion))
        center_x, center_y = (x1 + x2) / 2, (y1 + y2) / 2
        left, top = int(center_x - size / 2), int(center_y - size / 2)
        right, bottom = left + size, top + size
        source_left, source_top = max(0, left), max(0, top)
        source_right, source_bottom = min(width, right), min(height, bottom)
        if source_right <= source_left or source_bottom <= source_top:
            return None, face_size
        crop = image[source_top:source_bottom, source_left:source_right]
        crop = cv2.copyMakeBorder(
            crop, max(0, -top), max(0, bottom - height),
            max(0, -left), max(0, right - width), cv2.BORDER_REFLECT_101,
        )
        return crop, face_size

    @staticmethod
    def _preprocess(crop: np.ndarray) -> np.ndarray:
        target = 128
        height, width = crop.shape[:2]
        ratio = target / max(height, width)
        interpolation = cv2.INTER_LANCZOS4 if ratio > 1 else cv2.INTER_AREA
        resized = cv2.resize(
            crop, (max(1, int(width * ratio)), max(1, int(height * ratio))),
            interpolation=interpolation,
        )
        delta_w, delta_h = target - resized.shape[1], target - resized.shape[0]
        resized = cv2.copyMakeBorder(
            resized, delta_h // 2, delta_h - delta_h // 2,
            delta_w // 2, delta_w - delta_w // 2, cv2.BORDER_REFLECT_101,
        )
        # Frigate/OpenCV frames are BGR; the upstream model was trained on RGB.
        resized = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
        return np.transpose(resized, (2, 0, 1)).astype(np.float32)[None] / 255.0

    def analyze(self, image: np.ndarray, face) -> dict:
        if not self.enabled:
            return {"state": "disabled", "live": None, "score": None}
        crop, face_px = self._crop(image, face.bbox)
        if crop is None:
            return {
                "state": "insufficient", "live": None, "score": None,
                "face_px": face_px, "message": "face must be at least 64px",
            }
        try:
            session = self._runtime()
            input_name = session.get_inputs()[0].name
            logits = session.run(None, {input_name: self._preprocess(crop)})[0][0]
            if np.shape(logits) != (2,) or not np.isfinite(logits).all():
                raise ValueError("invalid liveness model output")
            logit_difference = float(logits[0]) - float(logits[1])
            if not math.isfinite(logit_difference):
                raise ValueError("invalid liveness model output")
            # Expose an intuitive 0..1 probability-like score while preserving
            # the upstream real-logit minus spoof-logit decision boundary.
            score = 1.0 / (1.0 + math.exp(-max(-50.0, min(50.0, logit_difference))))
            live = score >= self.threshold
            self.last_error = None
            return {
                "state": "live" if live else "spoof", "live": live,
                "score": round(score, 4), "threshold": self.threshold,
                "face_px": face_px,
            }
        except Exception as exc:
            self.last_error = str(exc)[:200]
            return {
                "state": "unavailable", "live": None, "score": None,
                "face_px": face_px, "message": self.last_error,
            }

    def consensus(self, history) -> dict:
        recent = deque(history, maxlen=self.required_frames)
        live_count = sum(item.get("live") is True for item in recent)
        spoof_count = sum(item.get("live") is False for item in recent)
        unavailable = any(item.get("state") == "unavailable" for item in recent)
        confirmed = (
            len(recent) == self.required_frames
            and live_count == self.required_frames
        )
        state = (
            "live" if confirmed else "unavailable" if unavailable
            else "spoof" if spoof_count else "pending"
        )
        scores = [item["score"] for item in recent if item.get("score") is not None]
        return {
            "state": state, "confirmed": confirmed,
            "live_frames": live_count, "spoof_frames": spoof_count,
            "required_frames": self.required_frames,
            "score": round(sum(scores) / len(scores), 4) if scores else None,
        }

    def status(self) -> dict:
        return {
            "enabled": self.enabled, "model_available": self.model_path.is_file(),
            "model_path": str(self.model_path), "threshold": self.threshold,
            "required_frames": self.required_frames, "last_error": self.last_error,
            "scope": "RGB print/screen attack reduction; not depth/IR certification",
        }


def qualified_live_consensus(detector, history, required=3):
    """Shared diagnostic/stream minimum; caller owns subject and frame uniqueness."""
    required=max(3,int(required),int(detector.required_frames))
    result=detector.consensus(history)
    confirmed=len(history)>=required and all(item.get('live') is True for item in history[-required:])
    result.update(confirmed=confirmed,required_frames=required,live_frames=len(history),
                  state='live' if confirmed else 'pending')
    return result
