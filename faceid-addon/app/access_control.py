"""Role and tab policy foundation; enforcement remains opt-in for 5.0 testing."""
from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path


ROLE_TABS = {
    "admin": ["*"],
    "operator": [
        "dashboard", "users", "guests", "site-map", "unknowns", "visits", "intercom", "liveness",
        "automations", "activity", "calibration", "privacy", "health-center",
    ],
    "viewer": ["dashboard", "site-map", "visits", "liveness", "activity"],
}
MUTATING_PREFIXES = {
    "operator": (
        "/api/persons", "/api/unknowns", "/api/audit",
        "/api/intercom", "/api/liveness", "/api/guests", "/api/site-map",
    ),
    "viewer": (),
}


class AccessControl:
    def __init__(self, path: Path, *, enabled: bool = False):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.enabled = bool(enabled)
        self._lock = threading.RLock()

    def _read(self) -> dict:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _write(self, value: dict) -> None:
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2)
                handle.flush(); os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def identity(headers) -> dict:
        return {
            "id": headers.get("x-remote-user-id") or "local",
            "name": (
                headers.get("x-remote-user-name")
                or headers.get("x-forwarded-user") or "Home Assistant"
            ),
        }

    def session(self, headers) -> dict:
        identity = self.identity(headers)
        assignments = self._read().get("assignments") or {}
        role = str(assignments.get(identity["id"], "admin"))
        if role not in ROLE_TABS:
            role = "viewer"
        return {
            **identity, "role": role, "tabs": ROLE_TABS[role],
            "enforced": self.enabled,
        }

    def allowed(self, headers, *, path: str, method: str) -> bool:
        if not self.enabled or method.upper() in {"GET", "HEAD", "OPTIONS"}:
            return True
        role = self.session(headers)["role"]
        if role == "admin":
            return True
        return any(path.startswith(prefix) for prefix in MUTATING_PREFIXES.get(role, ()))

    def policy(self) -> dict:
        return {
            "enabled": self.enabled,
            "roles": {role: {"tabs": tabs} for role, tabs in ROLE_TABS.items()},
            "note": "Role assignment UI is intentionally reserved for the next phase.",
        }
