import tempfile
import unittest
import json
from pathlib import Path

import numpy as np

from app.access_control import AccessControl
from app.liveness import LivenessDetector
from app.migrations import run_migrations


class ProductV5Tests(unittest.TestCase):
    class _FakeSession:
        def __init__(self, logits):
            self.logits = np.asarray([logits], dtype=np.float32)
            self.tensor = None

        def get_inputs(self):
            return [type("Input", (), {"name": "input"})()]

        def run(self, _outputs, inputs):
            self.tensor = inputs["input"]
            return [self.logits]

    def test_migration_is_backed_up_and_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "settings.json").write_text("{}", encoding="utf-8")
            first = run_migrations(root)
            self.assertTrue(first["changed"])
            self.assertTrue(Path(first["backup"]).is_file())
            second = run_migrations(root)
            self.assertFalse(second["changed"])
            self.assertEqual(second["to"], 5)

    def test_access_policy_is_safe_by_default_and_role_ready(self):
        with tempfile.TemporaryDirectory() as folder:
            service = AccessControl(Path(folder) / "access.json", enabled=False)
            session = service.session({"x-remote-user-id": "ronen", "x-remote-user-name": "Ronen"})
            self.assertEqual(session["role"], "admin")
            self.assertFalse(session["enforced"])
            self.assertTrue(service.allowed({}, path="/api/settings", method="POST"))

            service.path.write_text(json.dumps({"assignments": {"ronen": "viewer"}}), encoding="utf-8")
            service.enabled = True
            viewer = service.session({"x-remote-user-id": "ronen"})
            self.assertEqual(viewer["role"], "viewer")
            self.assertNotIn("settings", viewer["tabs"])
            self.assertFalse(service.allowed(
                {"x-remote-user-id": "ronen"}, path="/api/persons/alice", method="DELETE"
            ))

    def test_liveness_consensus_requires_consecutive_live_frames(self):
        detector = LivenessDetector(enabled=True, required_frames=3)
        pending = detector.consensus([
            {"state": "live", "live": True, "score": .8},
            {"state": "live", "live": True, "score": .9},
        ])
        self.assertFalse(pending["confirmed"])
        confirmed = detector.consensus([
            {"state": "spoof", "live": False, "score": .2},
            {"state": "live", "live": True, "score": .8},
            {"state": "live", "live": True, "score": .9},
            {"state": "live", "live": True, "score": 1.0},
        ])
        self.assertTrue(confirmed["confirmed"])

    def test_liveness_rejects_tiny_face_before_inference(self):
        detector = LivenessDetector(enabled=True)
        face = type("Face", (), {"bbox": np.asarray([10, 10, 50, 50])})()
        result = detector.analyze(np.zeros((100, 100, 3), dtype=np.uint8), face)
        self.assertEqual(result["state"], "insufficient")
        self.assertIsNone(result["live"])

    def test_liveness_inference_converts_bgr_to_rgb_and_exposes_probability(self):
        detector = LivenessDetector(enabled=True, threshold=.5)
        detector._session = self._FakeSession([2.0, -1.0])
        face = type("Face", (), {"bbox": np.asarray([20, 20, 100, 100])})()
        image = np.zeros((120, 120, 3), dtype=np.uint8)
        image[:, :, 2] = 255  # red in OpenCV BGR representation

        result = detector.analyze(image, face)

        self.assertTrue(result["live"])
        self.assertGreater(result["score"], .9)
        self.assertEqual(detector._session.tensor.shape, (1, 3, 128, 128))
        self.assertGreater(detector._session.tensor[0, 0].mean(), .9)
        self.assertLess(detector._session.tensor[0, 2].mean(), .1)


if __name__ == "__main__":
    unittest.main()
