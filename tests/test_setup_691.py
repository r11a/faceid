"""6.9.1: every Frigate camera is accepted on upgrade and watched; walk test sends no Access1 request."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from app import setup_wizard as setup
from app.camera_profiles import CameraProfiles
from app.mqtt_listener import EventProcessor
from tests.test_setup_startup import ENTERANCE, UpgradeParityTests, start
from tests.test_setup_wizard import URL, frigate_config


class AllCamerasTests(unittest.TestCase):
    def make(self, state="migrated", setup_value=None):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name)
        self.store = setup.SetupState(self.data)
        self.store.initialize(state == "migrated")
        if setup_value:
            self.store.update(lambda value: value.update(setup_value))
        self.config = frigate_config()
        self.probed = []
        self.used = {"Enterance"}

        def probe(url):
            self.probed.append(url)
            return {"ok": True, "codec": "hevc", "width": 2592, "height": 1944, "fps": 25}
        frigate = SimpleNamespace(config=lambda: self.config, live_url=lambda camera: f"{URL}/{camera}")
        self.monitor = setup.GeometryMonitor(self.data, self.store, frigate,
                                             CameraProfiles(self.data / "camera_profiles.json"),
                                             enabled=lambda camera: camera in self.used, probe=probe)
        return self.monitor

    def test_upgrade_accepts_cameras_that_are_off_without_opening_their_stream(self):
        monitor = self.make()
        monitor.check()
        value = self.store.read()
        for camera in ("Enterance", "Garden", "Shed"):
            self.assertTrue(monitor.box_association_allowed(camera), camera)
            self.assertEqual(value["cameras"][camera]["validated_by"], "upgrade")
        self.assertEqual(self.probed, [f"{URL}/Enterance"])
        self.assertIsNone(value["cameras"]["Garden"]["validated"]["stream"])

    def test_690_installation_accepts_the_cameras_it_skipped(self):
        # 6.9.0 accepted only cameras in use and marked the upgrade as checked.
        monitor = self.make(setup_value={"migration_checked": True, "cameras": {"Enterance": {
            "validated": {"source": "snapshots"}, "validated_by": "upgrade", "changes": []}}})
        self.assertTrue(monitor.box_association_allowed("Garden"))
        monitor.check()
        self.assertTrue(monitor.box_association_allowed("Garden"))
        self.assertEqual(self.store.read()["cameras"]["Garden"]["validated_by"], "upgrade")

    def test_camera_turned_on_later_keeps_association_and_fills_its_stream_size(self):
        monitor = self.make()
        monitor.check()
        self.used.add("Garden")
        monitor.check()
        self.assertTrue(monitor.box_association_allowed("Garden"))
        self.assertEqual(self.store.read()["cameras"]["Garden"]["validated"]["stream"], [2592, 1944])

    def test_change_while_off_is_caught_before_it_is_turned_on(self):
        monitor = self.make()
        monitor.check()
        self.config["cameras"]["Garden"]["detect"] = {"width": 1920, "height": 1080, "fps": 5}
        with self.assertLogs("faceid.setup", "WARNING"):
            monitor.check()
        self.assertFalse(monitor.box_association_allowed("Garden"))
        self.assertTrue(monitor.box_association_allowed("Enterance"))

    def test_camera_added_after_the_upgrade_needs_its_view_checked(self):
        monitor = self.make()
        monitor.check()
        self.config["cameras"]["Porch"] = {"detect": {"width": 1280, "height": 720}}
        monitor.check()
        self.assertFalse(monitor.box_association_allowed("Porch"))
        self.assertEqual(setup.camera_status(self.store.read(), "Porch")["status"], "unvalidated")

    def test_blank_install_still_needs_every_camera_checked(self):
        monitor = self.make(state="fresh")
        monitor.check()
        self.assertFalse(monitor.box_association_allowed("Garden"))
        self.assertFalse(monitor.box_association_allowed("Enterance"))


class DisabledCameraParityTests(unittest.TestCase):
    def test_effective_configuration_is_identical_after_the_first_check_and_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = UpgradeParityTests.make_687_install(None, root)
            balcony = {**ENTERANCE, "enabled": False, "observation_source": "snapshots", "role": "observation"}
            (data / "camera_profiles.json").write_text(json.dumps({"Enterance": ENTERANCE, "Balcony": balcony}))
            first, _, _, _ = start(root)
            config = frigate_config()
            config["cameras"]["Balcony"] = {"detect": {"width": 1280, "height": 720}}
            monitor = setup.GeometryMonitor(
                data, setup.SetupState(data), SimpleNamespace(config=lambda: config, live_url=lambda c: URL),
                CameraProfiles(data / "camera_profiles.json"), enabled=lambda c: c == "Enterance",
                probe=lambda url: {"ok": True, "codec": "hevc", "width": 2592, "height": 1944, "fps": 25})
            monitor.check()
            second, _, _, _ = start(root)
            self.assertEqual(first, second)
            self.assertTrue(second["cameras"]["Balcony"]["box_association"])
            self.assertTrue(second["cameras"]["Enterance"]["box_association"])


class WalkTestTests(unittest.TestCase):
    def setUp(self):
        self.now = [1000.0]
        self.test = setup.WalkTest(clock=lambda: self.now[0])
        self.decision = SimpleNamespace(person="David Levy", score=0.75, margin=0.6, slug="david")
        self.st = {"camera": "Enterance", "start_time": 997.4,
                   "liveness": {"confirmed": True, "score": 0.9, "live_frames": 2}}

    def test_records_recognition_and_liveness_only_while_running_on_that_camera(self):
        self.assertFalse(self.test.intercept("e0", self.st, self.decision))
        self.test.start("Enterance", 60)
        self.assertFalse(self.test.intercept("e1", {**self.st, "camera": "Balcony"}, self.decision))
        with self.assertLogs("faceid.setup", "INFO"):
            self.assertTrue(self.test.intercept("e2", self.st, self.decision))
        result = self.test.status()["results"][0]
        self.assertEqual((result["person"], result["liveness_confirmed"], result["recognized_after_seconds"]),
                         ("David Levy", True, 2.6))
        self.now[0] += 61
        self.assertFalse(self.test.intercept("e3", self.st, self.decision))
        self.assertFalse(self.test.status()["active"])

    def test_stop_and_limits(self):
        self.test.start("Enterance", 60)
        self.test.stop()
        self.assertFalse(self.test.active("Enterance"))
        for seconds in (5, 301, "60", None):
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                self.test.start("Enterance", seconds)

    def test_recognized_path_skips_access1_during_the_walk_test(self):
        processor = SimpleNamespace(access1=MagicMock(), setup_walk_test=self.test)
        submit = lambda: EventProcessor._submit_access1(processor, "e1", self.st, self.decision, "stream", None)
        self.test.start("Enterance", 60)
        self.assertIsNone(submit())
        processor.access1.enqueue.assert_not_called()
        self.test.stop()
        self.assertIs(submit(), processor.access1.enqueue.return_value)
        processor.access1.enqueue.assert_called_once()


if __name__ == "__main__":
    unittest.main()
