"""Optional local vision descriptions and semantic search; never identity decisions."""
import base64
import json
import logging
import queue
import threading

import cv2
import numpy as np
import requests

log = logging.getLogger("faceid.ai")


class AIContextService:
    def __init__(
        self, audit, *, enabled: bool = False, url: str = "http://localhost:11434",
        vision_model: str = "gemma3:4b", embedding_model: str = "embeddinggemma",
        timeout: float = 45,
    ):
        self.audit = audit
        self.enabled = bool(enabled)
        self.url = url.rstrip("/")
        self.vision_model = vision_model
        self.embedding_model = embedding_model
        self.timeout = float(timeout)
        # Frames are large; bounded tightly so a slow local model cannot exhaust RAM.
        self._queue: "queue.Queue[dict]" = queue.Queue(maxsize=20)
        self.last_error = None
        if self.enabled:
            threading.Thread(target=self._worker, daemon=True, name="faceid-ai").start()

    def submit(self, event_id: str, image, *, camera: str, decision: str):
        if not self.enabled or image is None:
            return False
        try:
            self._queue.put_nowait({
                "event_id": event_id, "image": image.copy(),
                "camera": camera, "decision": decision,
            })
            return True
        except queue.Full:
            log.warning("AI context queue full; dropped event %s", event_id)
            return False

    def _worker(self):
        while True:
            item = self._queue.get()
            try:
                description, tags = self._describe(item)
                embedding = self.embed(description)
                self.audit.update_context(
                    item["event_id"], description=description, tags=tags,
                    embedding=embedding,
                )
                self.last_error = None
            except Exception as exc:
                self.last_error = str(exc)
                log.warning("AI context failed for %s: %s", item["event_id"], exc)
            finally:
                self._queue.task_done()

    def _describe(self, item):
        ok, encoded = cv2.imencode(".jpg", item["image"])
        if not ok:
            raise ValueError("could not encode event frame")
        prompt = (
            "Describe only security-relevant visible facts. Do not identify a person. "
            "Return strict JSON with description (one sentence) and tags (short strings). "
            f"Camera={item['camera']}; face decision={item['decision']}."
        )
        response = requests.post(
            f"{self.url}/api/generate",
            json={
                "model": self.vision_model, "prompt": prompt, "stream": False,
                "format": "json",
                "images": [base64.b64encode(encoded.tobytes()).decode()],
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        content = response.json().get("response", "{}")
        parsed = json.loads(content) if isinstance(content, str) else content
        return str(parsed.get("description", "")).strip(), [
            str(tag)[:80] for tag in parsed.get("tags", [])[:12]
        ]

    def embed(self, text: str):
        if not text:
            return None
        response = requests.post(
            f"{self.url}/api/embed",
            json={"model": self.embedding_model, "input": text},
            timeout=self.timeout,
        )
        response.raise_for_status()
        data = response.json()
        vectors = data.get("embeddings") or []
        return vectors[0] if vectors else data.get("embedding")

    def search(self, query: str, limit: int = 50):
        rows = self.audit.context_events(limit=500)
        if not query.strip():
            for row in rows:
                row.pop("_embedding", None)
            return rows[:limit]
        vector = None
        if self.enabled:
            try:
                vector = self.embed(query)
            except Exception as exc:
                self.last_error = str(exc)
        if vector:
            needle = np.asarray(vector, dtype=np.float32)
            needle /= max(float(np.linalg.norm(needle)), 1e-8)
            for row in rows:
                stored = row.pop("_embedding", None)
                row["semantic_score"] = 0.0
                if stored and len(stored) == len(needle):
                    other = np.asarray(stored, dtype=np.float32)
                    other /= max(float(np.linalg.norm(other)), 1e-8)
                    row["semantic_score"] = round(float(needle @ other), 4)
            return sorted(rows, key=lambda row: row["semantic_score"], reverse=True)[:limit]
        lowered = query.casefold()
        matches = [
            row for row in rows
            if lowered in " ".join(str(row.get(k) or "") for k in (
                "person", "camera", "status", "ai_description", "ai_tags",
                "probable_person",
            )).casefold()
        ][:limit]
        for row in matches:
            row.pop("_embedding", None)
        return matches

    def health(self):
        return {
            "enabled": self.enabled,
            "queued": self._queue.qsize(),
            "last_error": self.last_error,
            "vision_model": self.vision_model if self.enabled else None,
        }
