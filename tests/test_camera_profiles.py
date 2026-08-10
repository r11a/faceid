import json
import tempfile
import unittest
from pathlib import Path

from app.camera_profiles import CameraProfiles


class CameraProfilesTests(unittest.TestCase):
    def test_defaults_persist_and_validate(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "camera_profiles.json"
            profiles = CameraProfiles(path, default_min_face_px=52)
            self.assertEqual(profiles.get("front")["min_face_px"], 52)
            saved = profiles.update("front", min_face_px=72, role="entry")
            self.assertEqual(saved["role"], "entry")
            self.assertEqual(json.loads(path.read_text())["front"]["min_face_px"], 72)
            with self.assertRaises(ValueError):
                profiles.update("front", min_face_px=12, role="entry")
            with self.assertRaises(ValueError):
                profiles.update("front", min_face_px=72, role="magic")


if __name__ == "__main__":
    unittest.main()
