"""Human-reviewed DINOv2 body appearance recognition.

Body evidence is deliberately advisory. It never changes the authoritative face
decision and therefore cannot unlock a door on its own.
"""
from __future__ import annotations

import json
import os
import pickle
import threading
import time
import uuid
from collections import Counter, defaultdict, deque
from pathlib import Path

import cv2
import numpy as np


def _atomic_json(path: Path, value: dict):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False), "utf-8")
    os.replace(temporary, path)


class CentroidClassifier:
    """Conservative bootstrap for a single resident; strangers remain unknown."""
    def __init__(self, name: str, center: np.ndarray):
        self.classes_ = np.asarray([name])
        self.center = center / max(float(np.linalg.norm(center)), 1e-9)

    def predict_proba(self, rows):
        values = []
        for row in rows:
            row = np.asarray(row)
            similarity = float(self.center @ (row / max(float(np.linalg.norm(row)), 1e-9)))
            values.append([max(0.0, min(1.0, (similarity + 1.0) / 2.0))])
        return np.asarray(values)


class BodyRecognitionService:
    def __init__(self, data_dir: Path, *, model_path: str = "/opt/faceid/models/dinov2-small.onnx",
                 enabled: bool = False, threshold: float = 0.72,
                 confirmations: int = 3, consensus_window: float = 300):
        self.root = data_dir / "body"
        self.pending = self.root / "pending"
        self.approved = self.root / "approved"
        self.strangers = self.root / "strangers"
        self.model_dir = self.root / "model"
        for path in (self.pending, self.approved, self.strangers, self.model_dir):
            path.mkdir(parents=True, exist_ok=True)
        self.model_path = Path(model_path)
        self.enabled = bool(enabled)
        self.threshold = float(threshold)
        self.confirmations = max(2, int(confirmations))
        self.consensus_window = max(30.0, float(consensus_window))
        self._session = None
        self._classifier = None
        self._classes = []
        self._lock = threading.RLock()
        self._votes: dict[str, deque] = defaultdict(deque)
        self.last_error = None
        self._load_classifier()

    def _load_classifier(self):
        path = self.model_dir / "classifier.pkl"
        if not path.is_file():
            return
        try:
            with path.open("rb") as handle:
                state = pickle.load(handle)
            self._classifier = state["classifier"]
            self._classes = state["classes"]
            self.threshold = float(state["threshold"])
        except Exception as exc:
            self.last_error = f"classifier load failed: {exc}"

    def _runtime(self):
        if self._session is None:
            if not self.model_path.is_file():
                raise RuntimeError("DINOv2 model is not installed")
            import onnxruntime as ort
            available = ort.get_available_providers()
            preferred = [name for name in (
                "CUDAExecutionProvider", "OpenVINOExecutionProvider", "CPUExecutionProvider"
            ) if name in available]
            self._session = ort.InferenceSession(str(self.model_path), providers=preferred)
        return self._session

    def embedding(self, image: np.ndarray) -> np.ndarray:
        rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        h, w = rgb.shape[:2]
        scale = 256.0 / min(h, w)
        resized = cv2.resize(rgb, (round(w * scale), round(h * scale)), interpolation=cv2.INTER_AREA)
        y, x = max(0, (resized.shape[0] - 224) // 2), max(0, (resized.shape[1] - 224) // 2)
        crop = resized[y:y + 224, x:x + 224].astype("float32") / 255.0
        crop = (crop - np.array([.485, .456, .406], dtype="float32")) / np.array([.229, .224, .225], dtype="float32")
        tensor = np.transpose(crop, (2, 0, 1))[None]
        output = self._runtime().run(None, {"pixel_values": tensor})[0]
        vector = output[:, 0, :].astype("float32")
        return vector[0] / max(float(np.linalg.norm(vector[0])), 1e-9)

    @staticmethod
    def quality(image: np.ndarray) -> dict:
        h, w = image.shape[:2]
        sharpness = float(cv2.Laplacian(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var())
        brightness = float(np.mean(image))
        usable = h >= 160 and w >= 60 and sharpness >= 35 and 25 <= brightness <= 235
        return {"usable": usable, "width": w, "height": h, "sharpness": round(sharpness, 1),
                "brightness": round(brightness, 1)}

    def add_pending(self, event_id: str, image: np.ndarray, suggested_person: str | None,
                    camera: str = "", source: str = "recognized-face") -> dict:
        for path in self.pending.glob("*.json"):
            try:
                if json.loads(path.read_text("utf-8")).get("event_id") == event_id:
                    return {"added": False, "reason": "event-already-staged"}
            except (OSError, ValueError):
                continue
        quality = self.quality(image)
        if not quality["usable"]:
            return {"added": False, "reason": "quality", "quality": quality}
        sample_id = uuid.uuid4().hex
        image_path = self.pending / f"{sample_id}.jpg"
        cv2.imwrite(str(image_path), image, [cv2.IMWRITE_JPEG_QUALITY, 91])
        meta = {"id": sample_id, "event_id": event_id, "suggested_person": suggested_person,
                "camera": camera, "source": source, "created": time.time(), "quality": quality}
        _atomic_json(self.pending / f"{sample_id}.json", meta)
        pending_meta = sorted(self.pending.glob("*.json"), key=lambda p: p.stat().st_mtime,
                              reverse=True)
        for old_meta in pending_meta[250:]:
            (self.pending / f"{old_meta.stem}.jpg").unlink(missing_ok=True)
            old_meta.unlink(missing_ok=True)
        return {"added": True, **meta}

    def materials(self) -> dict:
        pending = []
        for path in sorted(self.pending.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                row = json.loads(path.read_text("utf-8"))
                row["image"] = f"api/body/material/{row['id']}/image"
                pending.append(row)
            except (OSError, ValueError, KeyError):
                continue
        counts = {path.name: len(list(path.glob("*.jpg"))) for path in self.approved.iterdir() if path.is_dir()}
        return {"pending": pending, "approved": counts,
                "strangers": len(list(self.strangers.glob("*.jpg"))), "status": self.status()}

    def review(self, sample_id: str, action: str, person: str | None = None) -> bool:
        image = self.pending / f"{sample_id}.jpg"
        meta = self.pending / f"{sample_id}.json"
        if not image.is_file() or action not in ("approve", "stranger", "reject"):
            return False
        if action == "approve":
            safe = "".join(c for c in str(person or "") if c.isalnum() or c in "-_ ").strip()
            if not safe:
                return False
            target = self.approved / safe
            target.mkdir(exist_ok=True)
            os.replace(image, target / image.name)
        elif action == "stranger":
            os.replace(image, self.strangers / image.name)
        else:
            image.unlink(missing_ok=True)
        meta.unlink(missing_ok=True)
        return True

    def train(self) -> dict:
        from sklearn.svm import SVC
        rows, labels = [], []
        for person_dir in self.approved.iterdir():
            if not person_dir.is_dir():
                continue
            for path in person_dir.glob("*.jpg"):
                image = cv2.imread(str(path))
                if image is not None:
                    rows.append(self.embedding(image)); labels.append(person_dir.name)
        stranger_paths = list(self.strangers.glob("*.jpg"))
        if len(stranger_paths) >= 5:
            for path in stranger_paths:
                image = cv2.imread(str(path))
                if image is not None:
                    rows.append(self.embedding(image)); labels.append("__stranger__")
        counts = Counter(labels)
        residents = [name for name in counts if name != "__stranger__"]
        if not residents or any(counts[name] < 3 for name in residents):
            raise ValueError("Need at least 3 approved images for every learned person")
        x, y = np.asarray(rows), np.asarray(labels)
        if len(set(labels)) == 1:
            classifier = CentroidClassifier(residents[0], np.mean(x, axis=0))
            threshold = max(self.threshold, .86)
        else:
            classifier = SVC(probability=True, class_weight="balanced", random_state=7)
            classifier.fit(x, y)
            threshold = max(self.threshold, self._calibrate(x, y))
        state = {"classifier": classifier, "classes": classifier.classes_.tolist(),
                 "threshold": threshold, "trained": time.time(), "samples": counts}
        temporary = self.model_dir / "classifier.pkl.tmp"
        with temporary.open("wb") as handle:
            pickle.dump(state, handle, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(temporary, self.model_dir / "classifier.pkl")
        _atomic_json(self.model_dir / "status.json", {k: v for k, v in state.items() if k != "classifier"})
        self._classifier, self._classes, self.threshold = classifier, state["classes"], threshold
        return self.status()

    @staticmethod
    def _calibrate(x: np.ndarray, y: np.ndarray) -> float:
        from sklearn.model_selection import StratifiedKFold
        from sklearn.svm import SVC
        counts = Counter(y)
        splits = min(3, min(counts.values()))
        false_scores = []
        if splits < 2:
            return .72
        for seed in (7, 17, 27):
            for train, test in StratifiedKFold(splits, shuffle=True, random_state=seed).split(x, y):
                model = SVC(probability=True, class_weight="balanced", random_state=seed).fit(x[train], y[train])
                probs = model.predict_proba(x[test])
                for index, truth in enumerate(y[test]):
                    for column, candidate in enumerate(model.classes_):
                        if candidate not in (truth, "__stranger__"):
                            false_scores.append(float(probs[index, column]))
        return min(.99, max(.55, (max(false_scores) if false_scores else .70) + .02))

    def predict(self, image: np.ndarray, event_id: str) -> dict:
        if not self.enabled or self._classifier is None:
            return {"enabled": self.enabled, "armed": self._classifier is not None, "advisory": True}
        probabilities = self._classifier.predict_proba([self.embedding(image)])[0]
        index = int(np.argmax(probabilities)); person = str(self._classifier.classes_[index]); score = float(probabilities[index])
        accepted = score >= self.threshold and person != "__stranger__"
        now = time.time(); votes = self._votes[person]
        while votes and now - votes[0][0] > self.consensus_window:
            votes.popleft()
        if accepted and not any(row[1] == event_id for row in votes):
            votes.append((now, event_id))
        return {"enabled": True, "armed": True, "advisory": True,
                "person": person if accepted else None, "candidate": person,
                "score": score, "threshold": self.threshold, "confirmations": len(votes),
                "consensus": accepted and len(votes) >= self.confirmations}

    def status(self) -> dict:
        return {"enabled": self.enabled, "armed": self._classifier is not None,
                "model_available": self.model_path.is_file(), "threshold": self.threshold,
                "confirmations_required": self.confirmations, "classes": self._classes,
                "last_error": self.last_error, "authority": "advisory-only"}
