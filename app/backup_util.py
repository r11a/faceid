"""Backup der Galerie (persons + ignored) als tar.gz — geteilt von API und Auto-Scheduler."""
import io
import logging
import tarfile
import threading
import time
import json
import os
import sqlite3
import tempfile
from pathlib import Path

log = logging.getLogger("faceid.backup")

# Nur die unersetzliche Handarbeit sichern — nicht die Unknown-Queue oder Frigate-Vollbilder.
BACKUP_SUBDIRS = ("persons", "ignored", "body")
BACKUP_FILES = (
    "settings.json", "learning_runs.json", "frigate_sync.json",
    "camera_profiles.json",
)


def _safe_backup_member(info: tarfile.TarInfo):
    # sklearn's pickle is executable on load. Preserve reviewed material and status,
    # then retrain after restore instead of accepting an executable model in uploads.
    if info.name.endswith("/classifier.pkl"):
        return None
    return info


def _add_audit_snapshot(tar: tarfile.TarFile, audit: Path):
    """Use SQLite's online backup API so WAL-resident events are included."""
    descriptor, temporary = tempfile.mkstemp(prefix="faceid-audit-", suffix=".db")
    os.close(descriptor)
    try:
        source = sqlite3.connect(f"file:{audit.resolve()}?mode=ro", uri=True, timeout=15)
        destination = sqlite3.connect(temporary)
        try:
            source.backup(destination)
        finally:
            destination.close(); source.close()
        tar.add(temporary, arcname="system/audit.db")
    finally:
        Path(temporary).unlink(missing_ok=True)


def build_backup_gz(data_dir: Path) -> bytes:
    """Aktuelle Galerie als gzip-tar-Bytes."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for sub in BACKUP_SUBDIRS:
            d = data_dir / sub
            if d.exists():
                tar.add(d, arcname=sub, filter=_safe_backup_member)
        for name in BACKUP_FILES:
            path = data_dir / name
            if path.is_file():
                tar.add(path, arcname=f"system/{name}")
        audit = data_dir / "audit.db"
        if audit.is_file():
            _add_audit_snapshot(tar, audit)
        manifest = json.dumps({
            "format": 3, "created": time.time(),
            "includes": [*BACKUP_SUBDIRS, "settings", "learning-runs", "sync-ledger", "camera-profiles", "audit-history"],
            "excludes": ["frigate-credentials", "mqtt-credentials", "clips", "media-cache"],
            "restore_note": "body classifier is rebuilt from reviewed material after restore",
        }, indent=2).encode("utf-8")
        info = tarfile.TarInfo("manifest.json")
        info.size = len(manifest); info.mtime = int(time.time())
        tar.addfile(info, io.BytesIO(manifest))
    return buf.getvalue()


def write_backup_file(data_dir: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    path = backup_dir / f"faceid-backup-{ts}.tar.gz"
    path.write_bytes(build_backup_gz(data_dir))
    return path


def prune_backups(backup_dir: Path, keep: int):
    if keep <= 0:
        return
    files = sorted(backup_dir.glob("faceid-backup-*.tar.gz"), reverse=True)
    for old in files[keep:]:
        old.unlink(missing_ok=True)


def start_auto_backup(cfg_faceid: dict, data_dir: Path):
    """Täglicher Backup-Thread, wenn faceid.backup_enabled gesetzt ist.
    Liest die Config bei jedem Tick neu (Settings-Tab wirkt live)."""
    def loop():
        last_day = None
        while True:
            try:
                if cfg_faceid.get("backup_enabled"):
                    hour = int(cfg_faceid.get("backup_hour", 3))
                    now = time.localtime()
                    day = time.strftime("%Y-%m-%d", now)
                    if now.tm_hour >= hour and day != last_day:
                        backup_dir = Path(cfg_faceid.get("backup_dir") or (data_dir / "backups"))
                        p = write_backup_file(data_dir, backup_dir)
                        prune_backups(backup_dir, int(cfg_faceid.get("backup_keep", 7)))
                        last_day = day
                        log.info("auto backup written: %s", p)
            except Exception:
                log.exception("auto backup failed")
            time.sleep(300)  # alle 5 Min prüfen

    threading.Thread(target=loop, daemon=True, name="faceid-autobackup").start()
