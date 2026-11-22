"""6.9.0 setup endpoints. Every route requires the verified Home Assistant administrator."""
import logging
import os
import threading

import requests
from fastapi import Depends, HTTPException

from . import live_frames
from . import setup_wizard as setup
from .access1_admin import require_setup_admin

log = logging.getLogger("faceid.setup")


def register(app, cfg, engine, gallery, processor, data_dir):
    admin = [Depends(require_setup_admin)]
    state = setup.SetupState(data_dir)

    def geometry():
        return getattr(processor, "setup_geometry", None)

    def enabled(camera):
        check = getattr(processor, "camera_enabled", None)
        return bool(check(camera)) if callable(check) else True

    def known_camera(camera):
        config = processor.frigate.config() if getattr(processor, "frigate", None) else None
        names = set(((config or {}).get("cameras") or {}))
        profiles = getattr(processor, "camera_profiles", None)
        names |= set(profiles.all()) if profiles else set()
        if camera not in names:
            raise HTTPException(404, "Unknown camera")

    @app.get("/api/setup/status", dependencies=admin)
    def setup_status():
        value = state.read()
        return {"schema_version": value["schema_version"], "state": value["state"],
                "completed_at": value.get("completed_at"),
                "setup_mode": list(getattr(processor, "setup_missing", []) or []),
                "cameras": [setup.camera_status(value, camera) for camera in sorted(value["cameras"])],
                "rollback_available": setup.rollback_available(data_dir)}

    @app.get("/api/setup/discover", dependencies=admin)
    def setup_discover():
        return setup.discover(cfg, getattr(processor, "frigate", None), engine, enabled)

    @app.post("/api/setup/cameras/{camera}/probe", dependencies=admin)
    def setup_probe(camera: str):
        known_camera(camera)
        result = setup.probe_stream(processor.frigate.live_url(camera))
        if not result.get("ok"):
            # Manual fallback: the operator enters width / fps / codec instead.
            return {**result, "manual": True}
        return {**result, "recommendation": setup.recommend(result)}

    @app.post("/api/setup/recommend", dependencies=admin)
    def setup_recommend(body: dict):
        try:
            return setup.recommend({"width": body.get("width"), "fps": body.get("fps"),
                                    "codec": body.get("codec")})
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, str(exc)) from None

    @app.post("/api/setup/apply", dependencies=admin)
    def setup_apply(body: dict):
        profiles = getattr(processor, "camera_profiles", None)
        try:
            result = setup.apply_setup(data_dir, profiles, body)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        if profiles is not None:
            # Takes effect when each camera's stream reader is next opened.
            live_frames.CAMERA_DECODE = profiles.decode_map()
        log.info("setup: applied %s", ", ".join(result["applied"]))
        return result

    @app.post("/api/setup/rollback", dependencies=admin)
    def setup_rollback():
        try:
            result = setup.rollback(data_dir)
        except LookupError as exc:
            raise HTTPException(409, str(exc)) from None
        profiles = getattr(processor, "camera_profiles", None)
        if profiles is not None:
            live_frames.CAMERA_DECODE = profiles.decode_map()
        log.warning("setup: rolled back %s", ", ".join(result["restored"]))
        return {**result, "restart_required": True}

    @app.post("/api/setup/cameras/{camera}/validate", dependencies=admin)
    def setup_validate(camera: str):
        monitor = geometry()
        if monitor is None:
            raise HTTPException(503, "Geometry check unavailable")
        try:
            return monitor.accept(camera)
        except LookupError as exc:
            raise HTTPException(409, str(exc)) from None

    def walk_test():
        test = getattr(processor, "setup_walk_test", None)
        if test is None:
            raise HTTPException(503, "Walk test unavailable")
        return test

    @app.get("/api/setup/walk-test", dependencies=admin)
    def setup_walk_test_status():
        return walk_test().status()

    @app.post("/api/setup/walk-test", dependencies=admin)
    def setup_walk_test_start(body: dict):
        camera = body.get("camera")
        known_camera(camera)
        try:
            return walk_test().start(camera, body.get("seconds", 120))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.post("/api/setup/walk-test/stop", dependencies=admin)
    def setup_walk_test_stop():
        return walk_test().stop()

    # 6.9.3 site export / import (secret-free; import previewed, then journaled like apply).
    @app.get("/api/setup/export", dependencies=admin)
    def setup_export():
        from . import VERSION
        site = setup.export_site(cfg, data_dir, getattr(processor, "camera_profiles", None), VERSION)
        log.info("setup: site exported (%d cameras)", len(site["cameras"]))
        return site

    @app.post("/api/setup/import", dependencies=admin)
    def setup_import(body: dict):
        frigate = getattr(processor, "frigate", None)
        config = frigate.config() if frigate else None
        names = set((config or {}).get("cameras") or {}) if config else None
        profiles = getattr(processor, "camera_profiles", None)
        apply = body.get("apply") is True
        try:
            result = setup.import_site(cfg, data_dir, profiles, body.get("site"), names, apply=apply)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        if result["applied"]:
            if profiles is not None:
                live_frames.CAMERA_DECODE = profiles.decode_map()
            log.info("setup: site imported: %s", ", ".join(result["applied"]))
        return result

    @app.post("/api/setup/complete", dependencies=admin)
    def setup_complete():
        import time
        value = state.update(lambda v: v.update(state="completed", completed_at=time.time()))
        return {"state": value["state"], "completed_at": value["completed_at"]}

    @app.get("/api/setup/effective", dependencies=admin)
    def setup_effective():
        return setup.effective_config(cfg, processor, engine, gallery)

    @app.post("/api/setup/restart", dependencies=admin)
    def setup_restart():
        token = os.environ.get("SUPERVISOR_TOKEN", "")
        if not token:
            raise HTTPException(503, "Restart through Home Assistant is unavailable")

        def restart():
            try:
                requests.post("http://supervisor/addons/self/restart",
                              headers={"Authorization": f"Bearer {token}"}, timeout=10)
            except requests.RequestException:
                log.warning("setup: add-on restart request failed")
        threading.Timer(0.5, restart).start()
        return {"restarting": True}
