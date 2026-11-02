"""Optional stage timings. No images, names, credentials or request bodies."""
from contextlib import contextmanager
import logging
import time

log = logging.getLogger("faceid.timing")

def frame_wait(processor, event_id, state, reason):
    """At most one wait note per second/event; never change evidence state."""
    if getattr(processor, "timing_diagnostics", False) is not True:
        return
    now = time.monotonic()
    if now - state.get("timing_wait_logged", float("-inf")) < 1:
        return
    state["timing_wait_logged"] = now
    log.info("timing event=%s stage=fresh_frame_wait reason=%s", event_id, reason)

@contextmanager
def stage(processor, event_id, name):
    started = time.perf_counter()
    try:
        yield
    finally:
        seconds = max(0, time.perf_counter() - started)
        processor.record_timing(name, seconds)
        if getattr(processor, "timing_diagnostics", False) is True:
            log.info("timing event=%s stage=%s elapsed_ms=%.1f", event_id, name, seconds * 1000)
