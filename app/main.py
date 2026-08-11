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
from .frame_distributor import FrameDistributor
from .body_recognition import BodyRecognitionService
from .vision_advisor import VisionAdvisor
from .runtime_health import RuntimeHealth
from .camera_profiles import CameraProfiles
from .visits import VisitService
from .liveness import LivenessDetector
from .access_control import AccessControl
from .guest_access import GuestAccess
from .site_intelligence import SiteIntelligence
from .migrations import run_migrations

BASE = Path(__file__).resolve().parent.parent

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logbuffer.install()   # damit die Weboberflaeche das Log zeigen kann
log = logging.getLogger("faceid")


def main():
    cfg = yaml.safe_load((BASE / "config.yaml").read_text())
    data_dir = BASE / "data"
    pending_audit = data_dir / "audit.restore-pending"
    if pending_audit.is_file():
        pending_audit.replace(data_dir / "audit.db")
        log.warning("restored audit history from the previous backup before opening the database")
    migration = run_migrations(data_dir)
    if migration["changed"]:
        log.warning("data migration completed; recovery backup: %s", migration["backup"])
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
        max_clip_bytes=int(cfg["faceid"].get("media_max_clip_mb") or 150) * 1_000_000,
        max_cache_bytes=int(cfg["faceid"].get("media_cache_mb") or 1000) * 1_000_000,
        retention_hours=float(cfg["faceid"].get("media_retention_hours") or 24),
    )
    frame_distributor = FrameDistributor(
        data_dir, media_store,
        decode_mode=str(cfg["faceid"].get("video_decode", "auto")),
        max_frames=int(cfg["faceid"].get("clip_max_frames", 24)),
    )
    body_recognition = BodyRecognitionService(
        data_dir,
        model_path=str(cfg["faceid"].get("body_model_path") or "/opt/faceid/models/dinov2-small.onnx"),
        enabled=bool(cfg["faceid"].get("body_enabled", False)),
        threshold=float(cfg["faceid"].get("body_threshold", .72)),
        confirmations=int(cfg["faceid"].get("body_confirmations", 3)),
        consensus_window=float(cfg["faceid"].get("body_consensus_window", 300)),
    )
    vision_advisor = VisionAdvisor(
        data_dir, frame_distributor,
        enabled=bool(cfg["faceid"].get("vision_advisor_enabled", False)),
        url=str(cfg["faceid"].get("ai_url", "http://localhost:11434")),
        model=str(cfg["faceid"].get("ai_vision_model", "gemma3:4b")),
        timeout=float(cfg["faceid"].get("ai_timeout", 60)),
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
        frame_distributor=frame_distributor, body_recognition=body_recognition,
    )
    processor.frigate_sync = FrigateGallerySync(data_dir, gallery, engine, frigate)
    processor.camera_profiles = CameraProfiles(
        data_dir / "camera_profiles.json", default_min_face_px=processor.min_face_px
    )
    processor.visits = VisitService(
        audit, processor.camera_profiles,
        gap_minutes=int(f.get("visit_gap_minutes", 15)),
    )
    processor.access_control = AccessControl(
        data_dir / "access_control.json",
        enabled=bool(f.get("access_control_enabled", False)),
    )
    processor.guest_access = GuestAccess(
        data_dir,
        threshold=float(f.get("guest_match_threshold", max(.62, processor.match_thr + .08))),
        margin=float(f.get("guest_match_margin", max(.12, processor.match_margin))),
    )
    processor.site_intelligence = SiteIntelligence(
        data_dir / "site_map.json", audit, processor.camera_profiles, processor.visits,
    )
    processor.liveness = LivenessDetector(
        model_path=str(f.get("liveness_model_path") or "/opt/faceid/models/liveness.onnx"),
        enabled=bool(f.get("liveness_enabled", True)),
        threshold=float(f.get("liveness_threshold", 0.5)),
        required_frames=int(f.get("liveness_required_frames", 3)),
    )
    processor.migration = migration
    processor.vision_advisor = vision_advisor
    processor.runtime_health = RuntimeHealth(data_dir, processor)
    processor.start()
    start_auto_backup(cfg["faceid"], data_dir)
    app = build_app(cfg, engine, gallery, processor, data_dir, BASE / "static")
    uvicorn.run(app, host="0.0.0.0", port=int(cfg["faceid"].get("port", 8600)), log_level="warning")


if __name__ == "__main__":
    main()
