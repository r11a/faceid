"""Crash-safe local data migrations with a pre-migration recovery archive."""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path

from .backup_util import build_backup_gz


log = logging.getLogger("faceid.migrations")
SCHEMA_VERSION = 5


def _atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _current_schema(path: Path) -> int:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return max(0, int(value.get("schema", 0)))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return 0


def run_migrations(data_dir: Path, *, target: int = SCHEMA_VERSION) -> dict:
    """Mark the persistent layout only after a recovery archive is durable.

    Version 5 adds files rather than rewriting the face gallery, but creating the
    archive here protects future migrations too and makes the upgrade contract
    testable. Re-running after a crash is idempotent.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    schema_path = data_dir / "schema.json"
    current = _current_schema(schema_path)
    if current >= target:
        return {"from": current, "to": current, "backup": None, "changed": False}

    backup = None
    has_user_data = any((data_dir / name).exists() for name in (
        "persons", "ignored", "body", "audit.db", "settings.json",
    ))
    if has_user_data:
        backup_dir = data_dir / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / f"pre-migration-{current}-to-{target}.tar.gz"
        if not backup.exists():
            temporary = backup.with_name(f".{backup.name}.{uuid.uuid4().hex}.tmp")
            try:
                payload = build_backup_gz(data_dir)
                with temporary.open("wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, backup)
            finally:
                temporary.unlink(missing_ok=True)

    _atomic_json(schema_path, {
        "schema": int(target), "upgraded_from": int(current),
        "updated_ts": time.time(), "recovery_backup": str(backup) if backup else None,
    })
    log.info("persistent data schema upgraded %s -> %s (backup=%s)", current, target, backup)
    return {
        "from": current, "to": target, "backup": str(backup) if backup else None,
        "changed": True,
    }
