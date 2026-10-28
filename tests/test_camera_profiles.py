import json
import tempfile
import unittest
from pathlib import Path

from app.camera_profiles import CameraProfiles


class CameraProfilesTests(unittest.TestCase):
    def test_old_or_invalid_profiles_use_event_mode_and_safe_timing(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "camera_profiles.json"
            profiles = CameraProfiles(path)
            for stored in ({}, {"recognition_mode": "invalid"}, {"recognition_mode": []},
                           {"recognition_interval_seconds": "bad", "recognition_rearm_seconds": 0}):
                with self.subTest(stored=stored):
                    path.write_text(json.dumps({"door": stored}))
                    profile = profiles.get("door")
                    self.assertEqual(profile["recognition_mode"], "event")
                    self.assertEqual(profile["recognition_interval_seconds"], .5)
                    self.assertEqual(profile["recognition_rearm_seconds"], 2.)

    def test_recognition_settings_persist_per_camera_and_survive_omitted_updates(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "camera_profiles.json"
            profiles = CameraProfiles(path)
            for mode in ("event", "presence", "continuous"):
                profiles.update(mode, min_face_px=80, role="entry", recognition_mode=mode,
                                recognition_interval_seconds=.75, recognition_rearm_seconds=3.,
                                enabled=False, fast_recognition=True)
            profiles = CameraProfiles(path)
            for mode in ("event", "presence", "continuous"):
                with self.subTest(mode=mode):
                    result = profiles.update(mode, min_face_px=96, role="entry")
                    self.assertEqual(result["recognition_mode"], mode)
                    self.assertEqual(result["recognition_interval_seconds"], .75)
                    self.assertEqual(result["recognition_rearm_seconds"], 3.)
                    self.assertFalse(result["enabled"])
                    self.assertTrue(result["fast_recognition"])
            self.assertEqual(profiles.get("other")["recognition_mode"], "event")

    def test_invalid_recognition_settings_are_rejected_without_changing_profile(self):
        with tempfile.TemporaryDirectory() as folder:
            profiles = CameraProfiles(Path(folder) / "camera_profiles.json")
            profiles.update("door", min_face_px=80, role="entry", recognition_mode="presence")
            for field, values in {
                "recognition_mode": ["unknown", "", []],
                "recognition_interval_seconds": [0, .24, 5.01, float("nan"), float("inf")],
                "recognition_rearm_seconds": [0, .99, 30.01, float("nan"), float("inf")],
            }.items():
                for value in values:
                    with self.subTest(field=field, value=value):
                        with self.assertRaises(ValueError):
                            profiles.update("door", min_face_px=80, role="entry", **{field: value})
                        self.assertEqual(profiles.get("door")["recognition_mode"], "presence")
            for interval, rearm in ((.25, 1), (5, 30)):
                result = profiles.update("door", min_face_px=80, role="entry",
                                         recognition_interval_seconds=interval, recognition_rearm_seconds=rearm)
                self.assertEqual(result["recognition_interval_seconds"], interval)
                self.assertEqual(result["recognition_rearm_seconds"], rearm)

    def test_defaults_persist_and_validate(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "camera_profiles.json"
            profiles = CameraProfiles(path, default_min_face_px=52)
            self.assertEqual(profiles.get("front")["min_face_px"], 52)
            self.assertFalse(profiles.get("front")["fast_recognition"])
            profiles.update("front", min_face_px=52, role="entry", fast_recognition=True)
            self.assertTrue(profiles.get("front")["fast_recognition"])
            self.assertTrue(profiles.get("front")["enabled"])
            disabled = profiles.set_enabled("front", False)
            self.assertFalse(disabled["enabled"])
            saved = profiles.update("front", min_face_px=72, role="entry")
            self.assertTrue(saved["fast_recognition"])
            self.assertEqual(saved["role"], "entry")
            self.assertFalse(saved["enabled"])
            self.assertEqual(json.loads(path.read_text())["front"]["min_face_px"], 72)
            with self.assertRaises(ValueError):
                profiles.update("front", min_face_px=12, role="entry")
            with self.assertRaises(ValueError):
                profiles.update("front", min_face_px=72, role="magic")
            intercom = profiles.update(
                "door", min_face_px=120, night_min_face_px=96,
                role="intercom", mode="intercom", burst_frames=10,
                high_resolution=True, require_second_factor=True,
                roi=[.2, .1, .8, .95],
            )
            self.assertEqual(intercom["mode"], "intercom")
            self.assertEqual(intercom["liveness_mode"], "required")
            self.assertEqual(intercom["burst_frames"], 10)
            self.assertEqual(intercom["roi"], [.2, .1, .8, .95])
            with self.assertRaises(ValueError):
                profiles.update("door", min_face_px=120, role="intercom", liveness_mode="pretend")

if __name__ == "__main__":
    unittest.main()
