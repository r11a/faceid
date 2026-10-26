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
        self._seen_lock = threading.Lock()
        self._queue: "queue.Queue[dict]" = queue.Queue(maxsize=200)
        if self.webhook_urls:
            threading.Thread(
                target=self._worker, daemon=True, name="faceid-webhooks"
            ).start()

    def dispatch(self, payload: dict, *, client=None, prefix: str = "faceid",
                 topic: str = "v1/events", webhooks: bool = True, delivery=None):
        payload = {"schema_version": 1, **payload}
        key = topic + ":" + ":".join(str(payload.get(field, "")) for field in (
            "event_id", "decision", "person", "scenario_id"
        ))
        now = time.time()
        result = delivery if delivery is not None else {}
        result.update(mqtt=client is None, webhooks=not (webhooks and self.webhook_urls))
        sent = False
        with self._seen_lock:
            if len(self._seen) > 10000:
                cutoff = now - max(self.cooldown_seconds, 60)
                self._seen = {k: ts for k, ts in self._seen.items() if ts >= cutoff}
            mqtt_key, webhook_key = key + ":mqtt", key + ":webhook"
            if client:
                if now - self._seen.get(mqtt_key, 0) < self.cooldown_seconds:
                    result["mqtt"] = True
                else:
                    try:
                        info = client.publish(f"{prefix}/{topic}",
                            json.dumps(payload, ensure_ascii=False), retain=False)
                        rc = getattr(info, "rc", 0)
                        # Fakes may omit MQTTMessageInfo; real paho returns int rc.
                        if not isinstance(rc, int) or rc == 0:
                            self._seen[mqtt_key] = now
                            result["mqtt"] = True
                            sent = True
                            if payload.get("scenario"):
                                client.publish(f"{prefix}/v1/scenarios",
                                    json.dumps({"schema_version": 1, **payload["scenario"]}, ensure_ascii=False), retain=False)
                        else:
                            result["mqtt"] = False
                    except Exception:
                        # Unknown acceptance: never create a duplicate automation.
                        result["mqtt"] = None
                        self._seen[mqtt_key] = now
                        log.exception("MQTT publication uncertain for event %s", payload.get("event_id"))
            if webhooks and self.webhook_urls:
                if now - self._seen.get(webhook_key, 0) < self.cooldown_seconds:
                    result["webhooks"] = True
                else:
                    try:
                        self._queue.put_nowait(payload)
                        self._seen[webhook_key] = now
                        result["webhooks"] = True
                        sent = True
                    except queue.Full:
                        result["webhooks"] = False
                        log.warning("webhook queue full for event %s", payload.get("event_id"))
        return sent

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
