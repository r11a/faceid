"""Stable v1 automation events and optional non-blocking webhooks."""
import json
import logging
import queue
import threading
import time

log = logging.getLogger("faceid.integrations")


class IntegrationDispatcher:
    def __init__(self, *, webhook_urls=None, cooldown_seconds: float = 60):
        self.webhook_urls = [str(url) for url in (webhook_urls or []) if url]
        self.cooldown_seconds = float(cooldown_seconds)
        self._seen: dict[str, float] = {}
        self._queue: "queue.Queue[dict]" = queue.Queue(maxsize=200)
        if self.webhook_urls:
            threading.Thread(
                target=self._worker, daemon=True, name="faceid-webhooks"
            ).start()

    def dispatch(self, payload: dict, *, client=None, prefix: str = "faceid"):
        payload = {"schema_version": 1, **payload}
        key = ":".join(str(payload.get(field, "")) for field in (
            "event_id", "decision", "person", "scenario_id"
        ))
        now = time.time()
        if now - self._seen.get(key, 0) < self.cooldown_seconds:
            return False
        if len(self._seen) > 5000:
            cutoff = now - max(self.cooldown_seconds, 60)
            self._seen = {
                seen_key: seen_ts for seen_key, seen_ts in self._seen.items()
                if seen_ts >= cutoff
            }
        self._seen[key] = now
        if client:
            client.publish(
                f"{prefix}/v1/events",
                json.dumps(payload, ensure_ascii=False),
            )
            if payload.get("scenario"):
                client.publish(
                    f"{prefix}/v1/scenarios",
                    json.dumps(
                        {"schema_version": 1, **payload["scenario"]},
                        ensure_ascii=False,
                    ),
                )
        if self.webhook_urls:
            try:
                self._queue.put_nowait(payload)
            except queue.Full:
                log.warning("webhook queue full; dropped event %s", payload.get("event_id"))
        return True

    def _worker(self):
        import requests

        while True:
            payload = self._queue.get()
            try:
                for url in self.webhook_urls:
                    try:
                        requests.post(url, json=payload, timeout=8).raise_for_status()
                    except requests.RequestException as exc:
                        log.warning("webhook %s failed: %s", url, exc)
            finally:
                self._queue.task_done()

    def health(self):
        return {
            "webhooks": len(self.webhook_urls),
            "queued": self._queue.qsize(),
        }
