import sys
import os
from functools import partial
from pathlib import Path
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import cv2

from app.engine import FaceEngine


class EngineTests(unittest.TestCase):
    def make_engine(self, get=lambda image: []):
        # Model loading is external (downloads ~300 MB); test our wrapper boundary.
        session = SimpleNamespace(get_providers=lambda: ["CPUExecutionProvider"])
        model = SimpleNamespace(session=session, model_file="/models/w600k_r50.onnx")
        analysis = SimpleNamespace(
            prepare=lambda **kwargs: None, get=get,
            models={"recognition": model},
        )
        with patch.dict(sys.modules, {
            "insightface.app": SimpleNamespace(FaceAnalysis=lambda **kwargs: analysis),
            "onnxruntime": SimpleNamespace(get_available_providers=lambda: ["CPUExecutionProvider"]),
        }):
            return FaceEngine()

    def test_diagnostics_report_actual_session_provider_and_model(self):
        engine = self.make_engine()
        health = engine.health()
        self.assertIn("model_pack", health)
        self.assertEqual(health["model_pack"], "buffalo_l")
        self.assertEqual(health["models"]["recognition"], {
            "file": "w600k_r50.onnx", "providers": ["CPUExecutionProvider"],
        })
        self.assertIn("onnxruntime", health["runtime"])
        self.assertIsNone(health["inference"]["p95_ms"])

    def test_live_inference_overtakes_waiting_background_work(self):
        entered, release = threading.Event(), threading.Event()
        order = []
        def infer(image):
            order.append(int(image[0, 0, 0]))
            if len(order) == 1:
                entered.set()
                release.wait(3)
            return []
        engine = self.make_engine(infer)
        def background(value):
            with engine.background_work():
                return engine.faces(np.full((2, 2, 3), value, dtype=np.uint8))
        with ThreadPoolExecutor(max_workers=3) as pool:
            first = pool.submit(background, 0)
            try:
                self.assertTrue(entered.wait(1))
                second = pool.submit(background, 2)
                live = pool.submit(engine.faces, np.ones((2, 2, 3), dtype=np.uint8))
                with engine._lock._condition:
                    self.assertTrue(engine._lock._condition.wait_for(lambda: engine._lock._live_waiters == 1, timeout=1))
            finally:
                release.set()
            first.result(timeout=3); second.result(timeout=3); live.result(timeout=3)
        self.assertEqual(order, [0, 1, 2])

    def test_inference_timings_are_bounded_and_errors_propagate(self):
        image = np.zeros((2, 2, 3), dtype=np.uint8)
        engine = self.make_engine()
        clock = iter(value / 1000 for index in range(300) for value in (index * 100, index * 100 + 20))
        with patch("app.engine.time.perf_counter", side_effect=lambda: next(clock)):
            for _ in range(300):
                self.assertEqual(engine.faces(image), [])
        self.assertIn("inference", engine.health())
        metrics = engine.health()["inference"]
        self.assertEqual(metrics["calls"], 300)
        self.assertEqual(metrics["samples"], 256)
        self.assertEqual(metrics["failures"], 0)
        self.assertAlmostEqual(metrics["p50_ms"], 20, places=2)
        self.assertAlmostEqual(metrics["p95_ms"], 20, places=2)
        engine.app.get = lambda image: (_ for _ in ()).throw(ValueError("inference failed"))
        with self.assertRaisesRegex(ValueError, "inference failed"):
            engine.faces(image)
        self.assertEqual(engine.health()["inference"]["calls"], 301)
        self.assertEqual(engine.health()["inference"]["failures"], 1)

    def test_concurrent_calls_remain_serialized_and_health_does_not_wait_for_inference(self):
        entered, release = threading.Event(), threading.Event()
        def infer(image):
            entered.set()
            release.wait(5)
            return []
        engine = self.make_engine(infer)
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = pool.submit(engine.faces, np.zeros((2, 2, 3), dtype=np.uint8))
            self.assertTrue(entered.wait(2))
            try:
                health = pool.submit(engine.health).result(timeout=2)
                self.assertIn("inference", health)
                self.assertEqual(health["inference"]["calls"], 0)
            finally:
                release.set()
            self.assertEqual(pending.result(timeout=2), [])
        self.assertEqual(engine.health()["inference"]["calls"], 1)


@unittest.skipUnless(os.environ.get("FACEID_ENGINE_MODEL_ROOT"), "requires cached buffalo_l models")
class EnginePackageContractTests(unittest.TestCase):
    def test_actual_buffalo_l_inference_preserves_detection_and_embedding_contract(self):
        import insightface
        from insightface.app import FaceAnalysis

        # Explicit opt-in: never download models during the normal test suite.
        root = Path(os.environ["FACEID_ENGINE_MODEL_ROOT"]).resolve()
        self.assertTrue((root / "models/buffalo_l/det_10g.onnx").is_file())
        self.assertTrue((root / "models/buffalo_l/w600k_r50.onnx").is_file())
        with patch("insightface.app.FaceAnalysis", partial(FaceAnalysis, root=str(root))):
            engine = FaceEngine(backend="cpu")
        image = cv2.imread(str(Path(insightface.__file__).parent / "data/images/t1.jpg"))
        faces = engine.faces(image)
        self.assertEqual(len(faces), 6)
        # Baseline from genuine 0.7.3 source, same weights, CPU, 640 px, BGR.
        expected_boxes = np.array([
            [466.0821, 268.6164, 573.58923, 415.5331],
            [745.35284, 338.7414, 845.6634, 479.09033],
            [268.49826, 145.6026, 372.32516, 267.0035],
            [1132.9001, 269.0319, 1227.1696, 397.58008],
            [58.770386, 258.65308, 167.52798, 396.1947],
            [903.15533, 62.836594, 1013.3536, 205.22063],
        ])
        np.testing.assert_allclose([face.bbox for face in faces], expected_boxes, atol=.05, rtol=0)
        for face in faces:
            self.assertEqual(face.normed_embedding.shape, (512,))
            self.assertTrue(np.isfinite(face.normed_embedding).all())
            self.assertAlmostEqual(float(np.linalg.norm(face.normed_embedding)), 1., places=5)
        self.assertEqual(set(engine.app.models), {"detection", "recognition"})
        self.assertEqual(engine.app.addons, {})
        self.assertEqual(engine.health()["models"]["recognition"]["providers"], ["CPUExecutionProvider"])
        self.assertEqual(engine.health()["inference"]["calls"], 1)


if __name__ == "__main__":
    unittest.main()
