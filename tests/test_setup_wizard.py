"""6.9.0 setup backend: probe, recommendations, discovery, geometry guard, journaled apply."""
import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import live_frames
from app import setup_wizard as setup
from app.camera_profiles import CameraProfiles
from app.frigate_api import FrigateAPI
from app.stream_observation import StreamObservations

URL = "rtsp://user:secret@frigate.local:8554/Enterance"


def probe_output(codec="hevc", width=2592, height=1944, rate="25/1"):
    return json.dumps({"streams": [{"codec_name": codec, "width": width, "height": height,
                                    "avg_frame_rate": rate, "r_frame_rate": rate}]}).encode()


def frigate_config(width=1280, height=720, path="rtsp://u:*@cam/main", source="rtsp://u:*@cam/main"):
    return {
        "mqtt": {"topic_prefix": "frigate"},
        "go2rtc": {"streams": {"Enterance": [source], "Garden_sub": ["rtsp://garden/sub"]}},
        "cameras": {
            "Enterance": {"detect": {"width": width, "height": height, "fps": 5},
                          "ffmpeg": {"inputs": [{"path": path, "roles": ["detect", "record"]}]},
                          "objects": {"track": ["person"]}, "snapshots": {"enabled": True}},
            "Garden": {"detect": {"width": 640, "height": 360, "fps": 5},
                       "live": {"streams": {"Sub": "Garden_sub"}},
                       "objects": {"track": ["car"]}, "snapshots": {"enabled": False}},
            "Shed": {"detect": {"width": 640, "height": 480}},
        },
    }


class ProbeTests(unittest.TestCase):
    def run_probe(self, result=None, error=None):
        calls = []

        def run(args, **kwargs):
            calls.append((args, kwargs))
            if error:
                raise error
            return result
        return setup.probe_stream(URL, run=run), calls

    def test_parses_codec_size_and_rate_of_the_restream(self):
        result, calls = self.run_probe(SimpleNamespace(returncode=0, stdout=probe_output()))
        self.assertEqual(result, {"ok": True, "codec": "hevc", "width": 2592, "height": 1944, "fps": 25.0})
        args, kwargs = calls[0]
        self.assertEqual(args[0], "ffprobe")
        self.assertEqual(args[-1], URL)
        self.assertEqual(kwargs["timeout"], setup.PROBE_TIMEOUT)

    def test_failures_return_a_reason_and_never_the_address(self):
        cases = [
            (SimpleNamespace(returncode=1, stdout=b"", stderr=URL.encode()), None, "unreachable"),
            (None, subprocess.TimeoutExpired("ffprobe", 10), "timeout"),
            (None, FileNotFoundError(), "ffprobe_missing"),
            (SimpleNamespace(returncode=0, stdout=b"not json"), None, "unreadable"),
            (SimpleNamespace(returncode=0, stdout=b'{"streams": []}'), None, "unreadable"),
        ]
        for result, error, reason in cases:
            with self.subTest(reason=reason):
                value, _ = self.run_probe(result, error)
                self.assertEqual(value, {"ok": False, "reason": reason})
                self.assertNotIn("secret", json.dumps(value))

    def test_missing_restream_is_not_probed(self):
        self.assertEqual(setup.probe_stream(None, run=lambda *a, **k: self.fail("probed")),
                         {"ok": False, "reason": "no_restream"})

    def test_one_probe_at_a_time(self):
        with setup._probe_lock:
            self.assertEqual(setup.probe_stream(URL, timeout=0.01, run=lambda *a, **k: self.fail()),
                             {"ok": False, "reason": "busy"})


class RecommendTests(unittest.TestCase):
    def test_enterance_measurement_is_reproduced(self):
        rec = setup.recommend({"codec": "hevc", "width": 2592, "fps": 25}, gpu=True)
        self.assertEqual((rec["gpu_decode"], rec["decode_width"], rec["decode_step"]), (True, 1920, 3))
        self.assertEqual(rec["notes"], [])

    def test_other_cameras(self):
        cases = [
            ({"codec": "h264", "width": 1920, "fps": 15}, True, (True, 0, 2, [])),
            ({"codec": "h264", "width": 3840, "fps": 30}, True, (True, 1920, 4, [])),
            ({"codec": "h264", "width": 640, "fps": 10}, True, (True, 0, 1, ["low_resolution"])),
            ({"codec": "mjpeg", "width": 1280, "fps": 20}, True, (False, 0, 3, ["cpu_codec"])),
            ({"codec": "hevc", "width": 2560, "fps": None}, False, (False, 1920, 3, ["no_gpu"])),
        ]
        for facts, gpu, expected in cases:
            with self.subTest(facts=facts):
                rec = setup.recommend(facts, gpu=gpu)
                self.assertEqual((rec["gpu_decode"], rec["decode_width"], rec["decode_step"], rec["notes"]),
                                 expected)

    def test_width_is_required(self):
        with self.assertRaises(ValueError):
            setup.recommend({"codec": "h264", "fps": 25}, gpu=True)


class DiscoveryTests(unittest.TestCase):
    def test_cameras_restreams_and_warnings_without_addresses(self):
        cameras = setup.frigate_cameras(frigate_config(), enabled=lambda c: c == "Enterance")
        by_name = {c["camera"]: c for c in cameras}
        self.assertEqual(by_name["Enterance"]["restream"], "Enterance")
        self.assertEqual(by_name["Enterance"]["warnings"], [])
        self.assertTrue(by_name["Enterance"]["used_by_faceid"])
        self.assertEqual(by_name["Garden"]["restream"], "Garden_sub")
        self.assertEqual(by_name["Garden"]["warnings"], ["snapshots_disabled", "person_not_tracked"])
        self.assertIn("no_restream", by_name["Shed"]["warnings"])
        self.assertNotIn("rtsp", json.dumps(cameras))

    def test_alias_matches_the_live_reader_choice(self):
        config = frigate_config()
        api = FrigateAPI("http://frigate.local:5000")
        response = SimpleNamespace(status_code=200, json=lambda: config, close=lambda: None)
        with patch.object(api, "request", return_value=response):
            for name, camera in config["cameras"].items():
                self.assertEqual(api._main_stream(name),
                                 setup.restream_alias(name, camera, config["go2rtc"]["streams"]))

    def test_unreachable_frigate_offers_the_manual_path(self):
        cfg = {"frigate": {"url": "http://frigate.local:5000", "password": "pw"},
               "mqtt": {"host": "core-mosquitto", "password": "pw"}, "faceid": {}}
        result = setup.discover(cfg, SimpleNamespace(config=lambda: None))
        self.assertTrue(result["manual_fallback"])
        self.assertFalse(result["frigate"]["reachable"])
        self.assertNotIn('"pw"', json.dumps(result))

    def test_topic_prefix_mismatch_is_reported(self):
        cfg = {"frigate": {"url": "http://f:5000"}, "mqtt": {}, "faceid": {"frigate_topic_prefix": "nvr"}}
        result = setup.discover(cfg, SimpleNamespace(config=frigate_config))
        self.assertFalse(result["frigate"]["topic_prefix_matches"])


class ConnectionTests(unittest.TestCase):
    def test_saved_connection_wins_and_missing_connections_mean_setup_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            cfg = {"frigate": {"url": "https://192.168.1.10:8971", "username": "a"}, "mqtt": {"host": ""}}
            self.assertEqual(setup.missing_connections(cfg), ["mqtt"])
            (data / setup.CONNECTION_FILE).write_text(json.dumps(
                {"frigate": {"url": "http://frigate:5000", "password": ""},
                 "mqtt": {"host": "core-mosquitto", "port": 1883}}))
            self.assertEqual(setup.merge_connection(cfg, data), {"frigate": True, "mqtt": True})
            self.assertEqual(cfg["frigate"], {"url": "http://frigate:5000", "username": "a"})
            self.assertEqual(setup.missing_connections(cfg), [])
            self.assertEqual(setup.missing_connections({"frigate": {"url": "ftp://x"}, "mqtt": {"host": "m"}}),
                             ["frigate"])

    def test_connection_secrets_and_setup_files_stay_out_of_the_gallery_backup(self):
        import io
        import tarfile
        from app.backup_util import build_backup_gz
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            for name in (setup.CONNECTION_FILE, setup.SETUP_FILE, setup.KEY_FILE, "settings.json"):
                (data / name).write_text("{}")
            names = tarfile.open(fileobj=io.BytesIO(build_backup_gz(data)), mode="r:gz").getnames()
            self.assertIn("system/settings.json", names)
            for name in (setup.CONNECTION_FILE, setup.SETUP_FILE, setup.KEY_FILE):
                self.assertNotIn(f"system/{name}", names)


class GeometryTests(unittest.TestCase):
    def make(self, state="migrated", config=None, stream=(2592, 1944)):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        data = Path(self.tmp.name)
        self.config = config or frigate_config()
        self.stream = stream
        store = setup.SetupState(data)
        store.initialize(state == "migrated")
        profiles = CameraProfiles(data / "camera_profiles.json")
        frigate = SimpleNamespace(config=lambda: self.config, live_url=lambda camera: URL)

        def probe(url):
            return ({"ok": True, "codec": "hevc", "width": self.stream[0], "height": self.stream[1], "fps": 25}
                    if self.stream else {"ok": False, "reason": "timeout"})
        self.monitor = setup.GeometryMonitor(data, store, frigate, profiles,
                                             enabled=lambda c: c == "Enterance", probe=probe)
        self.data, self.store = data, store
        return self.monitor

    def status(self):
        return setup.camera_status(self.store.read(), "Enterance")

    def test_upgrade_keeps_box_association_and_accepts_the_first_check(self):
        monitor = self.make("migrated")
        self.assertTrue(monitor.box_association_allowed("Enterance"))
        self.assertEqual(self.status()["status"], "pending")
        monitor.check()
        self.assertTrue(monitor.box_association_allowed("Enterance"))
        status = self.status()
        self.assertEqual((status["status"], status["box_association"]), ("validated", True))
        self.assertEqual(self.store.read()["cameras"]["Enterance"]["validated_by"], "upgrade")

    def test_blank_install_needs_validation_before_box_association(self):
        monitor = self.make("fresh")
        self.assertFalse(monitor.box_association_allowed("Enterance"))
        monitor.check()
        self.assertFalse(monitor.box_association_allowed("Enterance"))
        self.assertEqual(self.status()["status"], "unvalidated")
        self.assertEqual(monitor.accept("Enterance")["status"], "validated")
        self.assertTrue(monitor.box_association_allowed("Enterance"))

    def test_changes_pause_box_association_and_say_why(self):
        for change, field in (("detect", "detect"), ("stream", "stream"),
                              ("source", "source_digest"), ("restream", "source_digest")):
            with self.subTest(change=change):
                monitor = self.make("migrated")
                monitor.check()
                if change == "detect":
                    self.config = frigate_config(width=1920, height=1080)
                elif change == "stream":
                    self.stream = (1920, 1080)
                elif change == "source":
                    self.config = frigate_config(path="rtsp://u:*@cam/other")
                else:
                    self.config = frigate_config(source="ffmpeg:rtsp://cam/main#video=h264")
                with self.assertLogs("faceid.setup", "WARNING"):
                    monitor.check()
                self.assertFalse(monitor.box_association_allowed("Enterance"))
                status = self.status()
                self.assertEqual(status["status"], "changed")
                self.assertIn(field, [item["field"] for item in status["changes"]])
                self.assertIn("may take longer", status["message"])
                monitor.accept("Enterance")
                self.assertTrue(monitor.box_association_allowed("Enterance"))

    def test_reverting_the_change_restores_association(self):
        monitor = self.make("migrated")
        monitor.check()
        self.config = frigate_config(width=1920, height=1080)
        with self.assertLogs("faceid.setup", "WARNING"):
            monitor.check()
        self.config = frigate_config()
        monitor.check()
        self.assertTrue(monitor.box_association_allowed("Enterance"))

    def test_failed_probe_is_not_a_change_and_is_filled_in_later(self):
        monitor = self.make("migrated", stream=None)
        monitor.check()
        self.assertIsNone(self.store.read()["cameras"]["Enterance"]["validated"]["stream"])
        self.stream = (2592, 1944)
        monitor.check()
        self.assertTrue(monitor.box_association_allowed("Enterance"))
        self.assertEqual(self.store.read()["cameras"]["Enterance"]["validated"]["stream"], [2592, 1944])

    def test_unreachable_frigate_keeps_previous_decisions(self):
        monitor = self.make("migrated")
        self.config = None
        monitor.frigate = SimpleNamespace(config=lambda: None, live_url=lambda c: URL)
        self.assertIsNone(monitor.check())
        self.assertTrue(monitor.box_association_allowed("Enterance"))

    def test_digest_is_keyed_and_stores_no_address(self):
        monitor = self.make("migrated")
        monitor.check()
        text = (self.data / setup.SETUP_FILE).read_text()
        self.assertNotIn("rtsp", text)
        self.assertEqual(len((self.data / setup.KEY_FILE).read_bytes()), 32)

    def test_stream_observation_honours_the_guard(self):
        allowed = {"value": False}
        processor = SimpleNamespace(
            setup_geometry=SimpleNamespace(box_association_allowed=lambda camera: allowed["value"]),
            frigate=SimpleNamespace(detect_size=lambda camera: self.fail("box mapped while paused")))
        fake = SimpleNamespace(processor=processor)
        self.assertFalse(StreamObservations._inside_box(fake, "Enterance", {"frame_time": 0}, {}, None))


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name)
        (self.data / "settings.json").write_text('{"match_threshold": 0.5}')
        self.profiles = CameraProfiles(self.data / "camera_profiles.json")
        self.profiles.update("Enterance", min_face_px=48, role="entry",
                             observation_source="frigate_restream", trust_local_video_network=True)
        setup.SetupState(self.data).initialize(True)
        self.before = self.snapshot()

    def snapshot(self):
        return {name: (self.data / name).read_text() if (self.data / name).exists() else None
                for name in setup.APPLY_FILES}

    def body(self):
        return {"connection": {"frigate": {"url": "http://frigate:5000", "password": "pw"}},
                "settings": {"stream_warm_seconds": 60},
                "cameras": {"Enterance": {"gpu_decode": True, "decode_width": 1920, "decode_step": 3}}}

    def test_apply_writes_every_section_and_keeps_the_profile(self):
        result = setup.apply_setup(self.data, self.profiles, self.body())
        self.assertTrue(result["restart_required"])
        self.assertEqual(self.profiles.decode_map(), {"Enterance": (True, 1920, 3)})
        profile = self.profiles.get("Enterance")
        self.assertEqual((profile["role"], profile["observation_source"]), ("entry", "frigate_restream"))
        self.assertEqual(json.loads((self.data / "settings.json").read_text()),
                         {"match_threshold": 0.5, "stream_warm_seconds": 60})
        self.assertFalse((self.data / setup.JOURNAL_FILE).exists())
        self.assertTrue(setup.rollback_available(self.data))

    def test_studio_save_keeps_the_setup_decode(self):
        setup.apply_setup(self.data, self.profiles, {"cameras": {"Enterance": {
            "gpu_decode": True, "decode_width": 1920, "decode_step": 3}}})
        self.profiles.update("Enterance", min_face_px=60, role="entry")
        self.assertEqual(self.profiles.get("Enterance")["decode"],
                         {"gpu_decode": True, "decode_width": 1920, "decode_step": 3})

    def test_invalid_input_writes_nothing(self):
        bad = [{"connection": {"frigate": {"url": "file:///etc/passwd"}}},
               {"settings": {"match_threshold": 0.1}},
               {"cameras": {"Enterance": {"gpu_decode": True, "decode_width": 99999, "decode_step": 3}}},
               {"cameras": {"Enterance": {"decode_width": 1920}}},
               {"unknown": {}}, {}]
        for body in bad:
            with self.subTest(body=body), self.assertRaises(ValueError):
                setup.apply_setup(self.data, self.profiles, body)
        self.assertEqual(self.snapshot(), self.before)

    def test_interrupted_apply_is_recovered_at_start(self):
        def crash(name):
            if name == "settings.json":
                raise RuntimeError("power loss")
        writes = {"settings.json": {"stream_warm_seconds": 5},
                  "camera_profiles.json": {}, "connection.json": {"mqtt": {"host": "x"}}}
        with self.assertRaises(RuntimeError):
            setup.transaction(self.data, writes, after_write=crash)
        self.assertTrue((self.data / setup.JOURNAL_FILE).exists())
        with self.assertLogs("faceid.setup", "WARNING"):
            self.assertTrue(setup.recover_interrupted_apply(self.data))
        self.assertEqual(self.snapshot(), self.before)
        self.assertFalse(setup.recover_interrupted_apply(self.data))

    def test_rollback_restores_the_previous_files_once(self):
        setup.apply_setup(self.data, self.profiles, self.body())
        self.assertNotEqual(self.snapshot(), self.before)
        setup.rollback(self.data)
        after = self.snapshot()
        self.assertEqual({k: v for k, v in after.items() if k != setup.SETUP_FILE},
                         {k: v for k, v in self.before.items() if k != setup.SETUP_FILE})
        self.assertEqual(json.loads(after[setup.SETUP_FILE]), json.loads(self.before[setup.SETUP_FILE]))
        self.assertIsNone(after[setup.CONNECTION_FILE])
        with self.assertRaises(LookupError):
            setup.rollback(self.data)


class ReaderDecodeTests(unittest.TestCase):
    def test_camera_without_override_uses_the_global_options(self):
        with patch.object(live_frames, "GPU_DECODE", True), patch.object(live_frames, "DECODE_WIDTH", 1920), \
                patch.object(live_frames, "DECODE_STEP", 3), patch.object(live_frames.os.path, "exists", return_value=True), \
                patch.object(live_frames, "VaapiCapture") as capture:
            live_frames.open_capture(URL)
            self.assertEqual((capture.call_args.kwargs["width"], capture.call_args.kwargs["step"]), (1920, 3))
            live_frames.open_capture(URL, (True, 0, 2))
            self.assertEqual((capture.call_args.kwargs["width"], capture.call_args.kwargs["step"]), (0, 2))

    def test_camera_override_can_choose_cpu(self):
        with patch.object(live_frames, "GPU_DECODE", True), \
                patch.object(live_frames, "VaapiCapture") as capture, \
                patch.object(live_frames.cv2, "VideoCapture") as cpu:
            live_frames.open_capture(URL, (False, 0, 3))
            capture.assert_not_called()
            cpu.assert_called_once()

    def test_pool_gives_each_new_reader_its_camera_decode(self):
        pool = live_frames.LiveFramePool()
        with patch.object(live_frames.LiveFrameReader, "start"), \
                patch.dict(live_frames.CAMERA_DECODE, {"Enterance": (True, 1920, 3)}, clear=True):
            self.assertEqual(pool.ensure("Enterance", URL).decode, (True, 1920, 3))
            self.assertIsNone(pool.ensure("Garden", URL + "2").decode)


if __name__ == "__main__":
    unittest.main()
