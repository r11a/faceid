"""Process-local, payload-free UI invalidation after committed audit writes."""
import asyncio
import threading

from fastapi import HTTPException
from fastapi.responses import StreamingResponse


class RevisionFeed:
    def __init__(self):
        self._lock = threading.Lock()
        self._revision = 0
        self._subscribers = {}

    @property
    def revision(self):
        with self._lock:
            return self._revision

    @property
    def subscriber_count(self):
        with self._lock:
            return len(self._subscribers)

    def publish(self):
        with self._lock:
            self._revision += 1
            for event, state in self._subscribers.items():
                if not state[1]:
                    state[1] = True
                    try:
                        state[0].call_soon_threadsafe(event.set)
                    except RuntimeError:  # The stream's event loop is shutting down.
                        pass

    async def stream(self, heartbeat=15):
        event = asyncio.Event()
        with self._lock:
            if len(self._subscribers) >= 256:
                raise HTTPException(503, "Too many update streams")
            self._subscribers[event] = [asyncio.get_running_loop(), False]
            revision = self._revision
        try:
            yield f"event: revision\ndata: {revision}\n\n"
            while True:
                try:
                    await asyncio.wait_for(event.wait(), timeout=heartbeat)
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                with self._lock:
                    event.clear()
                    self._subscribers[event][1] = False
                    revision = self._revision
                yield f"event: revision\ndata: {revision}\n\n"
        finally:
            with self._lock:
                self._subscribers.pop(event, None)


def register_realtime_routes(app, processor):
    @app.get("/api/updates")
    async def updates():
        audit = getattr(processor, "audit", None)
        if audit is None:
            raise HTTPException(503, "Audit updates unavailable")
        if audit.revisions.subscriber_count >= 256:
            raise HTTPException(503, "Too many update streams")
        return StreamingResponse(
            audit.revisions.stream(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-store", "X-Accel-Buffering": "no"},
        )
