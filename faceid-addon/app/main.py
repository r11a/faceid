"""FaceID — Gesichtserkennung für Frigate/HA. Start: python -m app.main"""
import json
import logging
from pathlib import Path

import uvicorn
import yaml

from . import logbuffer
from .engine import FaceEngine
from .frigate_api import FrigateAPI
from .gallery import Gallery
from .mqtt_listener import EventProcessor
from .webui import build_app
from .backup_util import start_auto_backup
from .audit import AuditStore
from .scenarios import ScenarioManager
from .reid import AppearanceReID
from .integrations import IntegrationDispatcher
from .ai_context import AIContextService
from .media_store import EventMediaStore
from .frigate_sync import FrigateGallerySync

BASE = Path(__file__).resolve().parent.parent

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logbuffer.install()   # damit die Weboberflaeche das Log zeigen kann
log = logging.getLogger("faceid")


def main():
    cfg = yaml.safe_load((BASE / "config.yaml").read_text())
    data_dir = BASE / "data"
    # Live-editierbare Einstellungen (Settings-Tab) liegen als Overlay in data/settings.json
    # und gewinnen über config.yaml — persistent auch beim Add-on (config.yaml wird dort
    # bei jedem Start neu generiert, /data überlebt).
    settings_f = data_dir / "settings.json"
    if settings_f.exists():
        try:
            cfg.setdefault("faceid", {}).update(json.loads(settings_f.read_text()))
        except (json.JSONDecodeError, OSError):
            log.warning("settings.json unreadable — ignoring it")
    log.info("loading InsightFace (buffalo_l) …")
    engine = FaceEngine(
        det_size=int(cfg["faceid"].get("det_size", 640)),
        backend=str(cfg["faceid"].get("backend", "auto")),
    )
    gallery = Gallery(data_dir,
                      top_k=int(cfg["faceid"].get("match_top_k", 3)),
                      max_per_person=int(cfg["faceid"].get("max_faces_per_person", 40)))
    gallery.trimmed_keep = int(cfg["faceid"].get("trimmed_keep", 10))
    gallery.dedupe_threshold = float(cfg["faceid"].get("dedupe_threshold", 0.65))
    frigate_cfg = cfg["frigate"]
    frigate = FrigateAPI(
        frigate_cfg["url"],
        username=str(frigate_cfg.get("username") or ""),
        password=str(frigate_cfg.get("password") or ""),
        verify_tls=bool(frigate_cfg.get("verify_tls", True)),
    )
    media_store = EventMediaStore(
        data_dir, frigate,
        max_clip_bytes=int(cfg["faceid"].get("media_max_clip_mb", 150)) * 1_000_000,
        max_cache_bytes=int(cfg["faceid"].get("media_cache_mb", 1000)) * 1_000_000,
        retention_hours=float(cfg["faceid"].get("media_retention_hours", 24)),
    )
    audit = AuditStore(
        data_dir / "audit.db",
        retention_days=int(cfg["faceid"].get("audit_retention_days", 90)),
    )
    audit.prune_evidence(
        int(cfg["faceid"].get("known_evidence_days", 30)),
        int(cfg["faceid"].get("unknown_evidence_days", 14)),
    )
    f = cfg["faceid"]
    camera_graph = f.get("camera_graph") or {}
    scenario_manager = ScenarioManager(
        audit,
        window_seconds=float(f.get("scenario_window", 90)),
        camera_graph=camera_graph,
    )
    reid = None
    if bool(f.get("reid_enabled", True)):
        reid = AppearanceReID(
            ttl_seconds=float(f.get("reid_ttl", 180)),
            threshold=float(f.get("reid_threshold", 0.90)),
            camera_graph=camera_graph,
        )
    dispatcher = IntegrationDispatcher(
        webhook_urls=f.get("webhook_urls") or [],
        cooldown_seconds=float(f.get("automation_cooldown", 60)),
    )
    ai_context = AIContextService(
        audit,
        enabled=bool(f.get("ai_enabled", False)),
        url=str(f.get("ai_url", "http://localhost:11434")),
        vision_model=str(f.get("ai_vision_model", "gemma3:4b")),
        embedding_model=str(f.get("ai_embedding_model", "embeddinggemma")),
        timeout=float(f.get("ai_timeout", 45)),
    )
    processor = EventProcessor(
        cfg, engine, gallery, frigate, audit=audit,
        scenario_manager=scenario_manager, reid=reid,
        dispatcher=dispatcher, ai_context=ai_context, media_store=media_store,
    )
    processor.frigate_sync = FrigateGallerySync(data_dir, gallery, engine, frigate)
    processor.start()
    start_auto_backup(cfg["faceid"], data_dir)
    app = build_app(cfg, engine, gallery, processor, data_dir, BASE / "static")
    uvicorn.run(app, host="0.0.0.0", port=int(cfg["faceid"].get("port", 8600)), log_level="warning")


if __name__ == "__main__":
    main()
