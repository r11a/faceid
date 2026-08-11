"""Small, honest self-checks for persistent storage and stalled work."""
from __future__ import annotations

import os
import time
import uuid
from pathlib import Path


class RuntimeHealth:
    def __init__(self, data_dir: Path, processor):
        self.data_dir = data_dir
        self.processor = processor
        self.started = time.time()

    def report(self) -> dict:
        checks, warnings = {}, []
        probe = self.data_dir / f".write-probe-{uuid.uuid4().hex}"
        try:
            probe.write_text("ok", "utf-8"); probe.unlink()
            checks["data_writable"] = True
        except OSError as exc:
            checks["data_writable"] = False; warnings.append(f"data is not writable: {exc}")
        checks["data_mount"] = self._mount_status()
        if checks["data_mount"] == "not-a-separate-mount":
            warnings.append("standalone data path is not a separate mount; an image replacement may lose learning data")
        queue_size = self.processor.queue.qsize()
        checks["processing_queue"] = queue_size
        if queue_size > 150:
            warnings.append("processing queue is close to capacity")
        checks["worker_threads"] = {
            "open_events": len(self.processor.events),
            "pending_jobs": len(self.processor.audit.pending_jobs()) if self.processor.audit else 0,
        }
        frigate = getattr(self.processor, "frigate", None)
        if frigate is not None:
            checks["frigate_security"] = {
                "secure_port": bool(frigate.secure_mode),
                "authenticated": bool(frigate.username),
                "tls_verified": bool(frigate.verify_tls),
            }
            if not frigate.secure_mode:
                warnings.append("Frigate uses the unauthenticated port; prefer port 8971")
            if frigate.secure_mode and not frigate.username:
                warnings.append("Frigate secure port is configured without a dedicated account")
            if not frigate.verify_tls:
                warnings.append("Frigate TLS certificate verification is disabled")
        checks["data_schema"] = getattr(self.processor, "migration", {}).get("to")
        return {"ok": not warnings, "uptime_seconds": round(time.time() - self.started),
                "checks": checks, "warnings": warnings}

    def _mount_status(self):
        if os.name != "posix":
            return "not-applicable"
        try:
            target = str(self.data_dir.resolve())
            mounts = Path("/proc/self/mountinfo").read_text("utf-8").splitlines()
            points = {line.split()[4] for line in mounts if len(line.split()) > 5}
            if target in points or target.startswith("/data"):
                return "persistent-volume"
            return "not-a-separate-mount"
        except OSError:
            return "unknown"
