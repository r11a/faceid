"""6.9.0 acceptance at startup: upgrade parity with 6.8.7, blank install, guarded setup routes."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import main as main_module
from app import setup_api
from app import setup_wizard as setup
from app.access1_admin import require_setup_admin

BASELINE_687 = {
    "frigate": {"url": "https://192.168.1.10:8971", "username": "faceid", "password": "frigate-secret",
                "verify_tls": False},
    "mqtt": {"host": "core-mosquitto", "port": 1883, "user": "addons", "password": "mqtt-secret"},
    "faceid": {"backend": "openvino_gpu", "stream_warm_seconds": 60, "stream_decode_threads": 2,
               "stream_gpu_decode": True, "stream_decode_width": 1920, "frigate_box_association": True,
               "liveness_enabled": True, "liveness_threshold": 0.5, "liveness_required_frames": 3,
               "match_threshold": 0.5, "unknown_threshold": 0.35, "match_margin": 0.08,
               "min_confirmations": 2, "camera_graph": "{}"},
}
ENTERANCE = {"observation_source": "frigate_restream", "trust_local_video_network": True,
             "direct_rtsp_url": "", "enabled": True, "min_face_px": 48, "role": "entry",
             "mode": "standard", "liveness_mode": "required", "recognition_mode": "event",
             "roi": [0.0, 0.0, 1.0, 1.0]}


def start(root, *, processor_start=None):
    """One add-on start through the real main(); returns (effective config, served app, start mock)."""
    engine = SimpleNamespace(faces=lambda image: [], active_backend="openvino_gpu", native_device="GPU")
    with patch("app.diagnostic_mode.DIAGNOSTIC_ONLY", False), \
            patch.object(main_module, "DIAGNOSTIC_ONLY", False), \
            patch.object(main_module, "OPENVINO_TEST_BUILD", False), \
            patch.object(main_module, "BASE", root), \
            patch.object(main_module, "FaceEngine", return_value=engine), \
            patch.object(main_module.EventProcessor, "start") as started, \
            patch("app.access1.Access1Integration") as connector, \
            patch.object(setup.GeometryMonitor, "start"), \
            patch.object(main_module, "start_auto_backup"), \
            patch.object(main_module.uvicorn, "run") as serve, \
            patch.object(main_module.log, "info") as info:
        connector.return_value.status.return_value = {
            "enabled": True, "state": "ready", "secure": True, "directory_running": True}
        main_module.main()
    effective = next(json.loads(call.args[1]) for call in info.call_args_list
                     if call.args and call.args[0] == "effective configuration: %s")
    return effective, serve.call_args.args[0], started, connector


class UpgradeParityTests(unittest.TestCase):
    def make_687_install(self, root):
        (root / "config.yaml").write_text(yaml.safe_dump(BASELINE_687), encoding="utf-8")
        data = root / "data"
        data.mkdir()
        (data / "audit.db").touch()
        (data / "camera_profiles.json").write_text(json.dumps({"Enterance": ENTERANCE}), encoding="utf-8")
        (data / "settings.json").write_text(json.dumps({"match_threshold": 0.5}), encoding="utf-8")
        return data

    def test_687_settings_are_unchanged_across_upgrade_and_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = self.make_687_install(root)
            live_files = {name: (data / name).read_bytes() for name in ("camera_profiles.json", "settings.json")}
            first, app, started, connector = start(root)
            second, _, _, _ = start(root)
            self.assertEqual(first, second)
            started.assert_called_once()
            connector.assert_called_once()
            # The upgrade writes no live setting; only setup.json is new.
            self.assertEqual({name: (data / name).read_bytes() for name in live_files}, live_files)
            self.assertFalse((data / setup.CONNECTION_FILE).exists())
            self.assertEqual(setup.SetupState(data).read()["state"], "migrated")

            self.assertEqual(first["engine"]["active_backend"], "openvino_gpu")
            self.assertEqual(first["options"]["backend"], "openvino_gpu")
            self.assertEqual(first["reader"], {"gpu_decode": True, "decode_width": 1920,
                                               "decode_step": 3, "warm_seconds": 60.0})
            self.assertTrue(first["box_association"])
            camera = first["cameras"]["Enterance"]
            self.assertEqual(camera["decode"], {"gpu_decode": True, "decode_width": 1920, "decode_step": 3})
            self.assertEqual(camera["decode_source"], "global")
            self.assertTrue(camera["box_association"])
            self.assertEqual((camera["observation_source"], camera["trust_local_video_network"],
                              camera["role"], camera["liveness_mode"]),
                             ("frigate_restream", True, "entry", "required"))
            self.assertEqual((first["options"]["liveness_enabled"], first["options"]["liveness_threshold"],
                              first["options"]["liveness_required_frames"]), (True, 0.5, 3))
            self.assertEqual(first["setup_mode"], [])
            text = json.dumps(first)
            self.assertNotIn("secret", text)
            self.assertTrue(first["connection"]["frigate"]["password_set"])

    def test_interrupted_apply_is_undone_before_settings_are_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = self.make_687_install(root)
            setup.SetupState(data).initialize(True)
            with self.assertRaises(RuntimeError):
                setup.transaction(data, {"settings.json": {"stream_warm_seconds": 5}},
                                  after_write=lambda name: (_ for _ in ()).throw(RuntimeError("power loss")))
            effective, _, _, _ = start(root)
            self.assertEqual(effective["options"]["stream_warm_seconds"], 60)
            self.assertEqual(json.loads((data / "settings.json").read_text()), {"match_threshold": 0.5})

    def test_rollback_after_apply_restores_the_687_effective_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = self.make_687_install(root)
            before, _, _, _ = start(root)
            from app.camera_profiles import CameraProfiles
            setup.apply_setup(data, CameraProfiles(data / "camera_profiles.json"), {
                "settings": {"stream_warm_seconds": 30},
                "cameras": {"Enterance": {"gpu_decode": True, "decode_width": 0, "decode_step": 2}}})
            applied, _, _, _ = start(root)
            self.assertEqual(applied["reader"]["warm_seconds"], 30.0)
            self.assertEqual(applied["cameras"]["Enterance"]["decode_source"], "camera")
            self.assertEqual(applied["cameras"]["Enterance"]["decode"]["decode_step"], 2)
            setup.rollback(data)
            after, _, _, _ = start(root)
            self.assertEqual(after, before)


class BlankInstallTests(unittest.TestCase):
    def test_starts_in_setup_mode_with_setup_routes_and_no_recognition(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config.yaml").write_text(yaml.safe_dump({
                "frigate": {"url": "https://192.168.1.10:8971", "username": "", "password": ""},
                "mqtt": {"host": "", "port": 1883},
                "faceid": {"liveness_enabled": False, "camera_graph": "{}"}}), encoding="utf-8")
            effective, app, started, connector = start(root)
            started.assert_not_called()
            connector.assert_not_called()
            self.assertEqual(effective["setup_mode"], ["mqtt"])
            self.assertEqual(setup.SetupState(root / "data").read()["state"], "fresh")
            paths = {route.path for route in app.routes}
            self.assertTrue({"/api/setup/status", "/api/setup/discover", "/api/setup/apply",
                             "/api/setup/rollback", "/api/setup/effective"} <= paths)

    def test_saved_connection_leaves_setup_mode_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config.yaml").write_text(yaml.safe_dump({
                "frigate": {"url": "https://192.168.1.10:8971"}, "mqtt": {"host": ""},
                "faceid": {"liveness_enabled": False, "camera_graph": "{}"}}), encoding="utf-8")
            start(root)
            setup.apply_setup(root / "data", None, {"connection": {
                "mqtt": {"host": "core-mosquitto", "port": 1883, "password": "pw"}}})
            effective, _, started, _ = start(root)
            started.assert_called_once()
            self.assertEqual(effective["setup_mode"], [])
            self.assertEqual(effective["connection"]["mqtt"]["host"], "core-mosquitto")


class SetupRouteGuardTests(unittest.TestCase):
    def make_app(self, data, admin):
        app = FastAPI()
        frigate = SimpleNamespace(config=lambda: None, live_url=lambda camera: None)
        processor = SimpleNamespace(frigate=frigate, camera_profiles=None, setup_missing=["mqtt"],
                                    camera_enabled=lambda camera: True)
        cfg = {"frigate": {"url": "http://f:5000", "password": "frigate-secret"},
               "mqtt": {"host": "", "password": "mqtt-secret"}, "faceid": {}}
        setup_api.register(app, cfg, None, None, processor, data)
        if admin:
            app.dependency_overrides[require_setup_admin] = lambda: "a" * 32
        return TestClient(app)

    def test_every_setup_route_requires_the_verified_administrator(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = self.make_app(Path(tmp), admin=False)
            for method, path in (("get", "/api/setup/status"), ("get", "/api/setup/discover"),
                                 ("get", "/api/setup/effective"), ("post", "/api/setup/apply"),
                                 ("post", "/api/setup/rollback"), ("post", "/api/setup/complete"),
                                 ("post", "/api/setup/restart"), ("post", "/api/setup/recommend"),
                                 ("post", "/api/setup/cameras/Enterance/probe"),
                                 ("post", "/api/setup/cameras/Enterance/validate"),
                                 ("get", "/api/setup/export"), ("post", "/api/setup/import")):
                with self.subTest(path=path):
                    response = (client.get(path) if method == "get" else client.post(path, json={}))
                    self.assertEqual(response.status_code, 403)

    def test_admin_sees_status_and_discovery_without_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            setup.SetupState(data).initialize(False)
            client = self.make_app(data, admin=True)
            status = client.get("/api/setup/status").json()
            self.assertEqual((status["state"], status["setup_mode"]), ("fresh", ["mqtt"]))
            discovery = client.get("/api/setup/discover").json()
            self.assertTrue(discovery["manual_fallback"])
            self.assertNotIn("secret", json.dumps(discovery))
            self.assertEqual(client.post("/api/setup/cameras/Nope/probe").status_code, 404)
            self.assertEqual(client.post("/api/setup/recommend",
                                         json={"width": 2592, "fps": 25, "codec": "hevc"}).status_code, 200)
            self.assertEqual(client.post("/api/setup/apply", json={"settings": {"x": 1}}).status_code, 400)
            self.assertEqual(client.post("/api/setup/rollback").status_code, 409)
            applied = client.post("/api/setup/apply", json={"connection": {"mqtt": {"host": "core-mosquitto"}}})
            self.assertEqual(applied.json(), {"applied": ["connection.json"], "restart_required": True})
            self.assertEqual(client.post("/api/setup/rollback").status_code, 200)
            self.assertFalse((data / setup.CONNECTION_FILE).exists())
            # 6.9.3 site file: exported without secrets, refused when malformed, previewed by default.
            site = client.get("/api/setup/export").json()
            self.assertEqual((site["format"], site["version"]), ("faceid-site", 1))
            self.assertEqual(client.post("/api/setup/import", json={"site": {"format": "x"}}).status_code, 400)
            preview = client.post("/api/setup/import", json={"site": site}).json()
            self.assertEqual((preview["changes"], preview["applied"]), (False, []))



class SetupAdminHeaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_setup_writes_need_their_own_confirm_header(self):
        from unittest.mock import AsyncMock
        from fastapi import HTTPException
        from starlette.requests import Request
        from app import access1_admin as admin

        def request(header):
            values = {"x-remote-user-id": "a" * 32, "x-ingress-path": "/api/hassio_ingress/x",
                      "content-type": "application/json", header: "1"}
            return Request({"type": "http", "method": "POST", "path": "/api/setup/apply",
                            "client": ("172.30.32.2", 1), "headers": [(k.encode(), v.encode()) for k, v in values.items()]})
        with patch.object(admin, "administrator", new_callable=AsyncMock, return_value=True):
            with self.assertRaises(HTTPException) as error:
                await admin.require_setup_admin(request("x-access1-pairing"))
            self.assertEqual(error.exception.status_code, 403)
            self.assertEqual(await admin.require_setup_admin(request("x-faceid-setup")), "a" * 32)
            self.assertEqual(await admin.require_pairing_admin(request("x-access1-pairing")), "a" * 32)
        with patch.object(admin, "administrator", new_callable=AsyncMock, return_value=False):
            with self.assertRaises(HTTPException) as error:
                await admin.require_setup_admin(request("x-faceid-setup"))
            self.assertEqual(error.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
