import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from app.engine import FaceEngine
from app.native_openvino import NativeSession
from app.stream_observation import StreamObservations


class NativeOpenVINOTests(unittest.TestCase):
    def test_named_outputs_keep_onnx_order_and_support_batch(self):
        model = SimpleNamespace(model_file="fixture.onnx", session=SimpleNamespace(
            get_inputs=lambda: [SimpleNamespace(name="image")],
            get_outputs=lambda: [SimpleNamespace(name=name) for name in ("scores", "boxes")]))
        dim = SimpleNamespace(is_static=True, get_length=lambda: 1)

        class Compiled:
            def get_property(self, name):
                return ["GPU.0"]

            def input(self, name):
                return port

            def output(self, name):
                return name

            def __call__(self, inputs):
                value = list(inputs.values())[0]
                return {"boxes": value + 2, "scores": value + 1}

        class Input:
            partial_shape = [dim]

        port = Input()
        core = SimpleNamespace(read_model=lambda path: object(), compile_model=lambda *args: Compiled())
        adapter = NativeSession(core, model, "GPU", None)
        batch = np.array([[0], [10]], np.float32)
        scores, boxes = adapter.run(None, {"image": batch})
        np.testing.assert_array_equal(scores, [[1], [11]])
        np.testing.assert_array_equal(boxes, [[2], [12]])
        np.testing.assert_array_equal(adapter.run(["boxes"], {"image": batch})[0], boxes)

    def test_mid_event_failure_restores_sessions_and_invalidates_active_event(self):
        engine = FaceEngine.__new__(FaceEngine)
        engine.backend_generation = 0
        engine.native_device = "GPU.0"
        engine._native_originals = {"detection": Mock(), "recognition": Mock()}
        engine.app = SimpleNamespace(models={task: SimpleNamespace(session=Mock())
                                            for task in engine._native_originals})
        engine._native_detector_static_shapes = True
        engine.app.models["detection"].static_shape_sessions = False
        engine.models = {task: {} for task in engine._native_originals}
        processor = SimpleNamespace(engine=engine)
        streams = StreamObservations(processor)
        session = {"eid": "old", "backend_generation": 0}
        streams.sessions["old"] = session
        with patch.object(engine, "_select_native") as select:
            engine._native_failed()
        select.assert_called_once_with(("CPU",))
        for task, original in engine._native_originals.items():
            self.assertIs(engine.app.models[task].session, original)
        self.assertTrue(engine.app.models["detection"].static_shape_sessions)
        self.assertEqual(streams.valid(session), "inference_backend_changed")


if __name__ == "__main__":
    unittest.main()
