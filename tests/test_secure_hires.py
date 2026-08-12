import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

try:
    from app.mqtt_listener import EventProcessor
except ModuleNotFoundError:
    EventProcessor = None


class Engine:
    def __init__(self, detected_face):
        self.detected_face = detected_face

    def faces(self, _image):
        return [self.detected_face]


class Frigate:
    def __init__(self):
        self.recording_calls = []
        self.snapshot_calls = []

    def recording_frame(self, camera, timestamp):
        self.recording_calls.append((camera, timestamp))
        return np.zeros((200, 200, 3), dtype=np.uint8)

    def snapshot(self, event_id, crop=True):
        self.snapshot_calls.append((event_id, crop))
        return None


@unittest.skipIf(EventProcessor is None, "MQTT runtime dependencies are not installed")
class SecureHighResolutionTests(unittest.TestCase):
    def test_selected_camera_uses_authenticated_frigate_recording_path(self):
        detected = SimpleNamespace(
            bbox=np.asarray([20, 20, 140, 140], dtype=np.float32),
            normed_embedding=np.asarray([1.0, 0.0], dtype=np.float32),
        )
        frigate = Frigate()
        processor = EventProcessor(
            {"faceid": {}}, Engine(detected), object(), frigate,
        )
        processor.camera_profiles = SimpleNamespace(get=lambda _camera: {
            "mode": "intercom", "high_resolution": True,
            "min_face_px": 100, "night_min_face_px": 100,
            "roi": [0, 0, 1, 1],
        })
        processor.events["event-1"] = processor._new_event_state(
            "event-1", "door", start_time=10,
        )
        processor._process_face = Mock()
        quality = SimpleNamespace(usable=True, score=.9)
        with patch("app.mqtt_listener.measure_face_quality", return_value=quality):
            processor._process("event-1")

        self.assertEqual(len(frigate.recording_calls), 1)
        self.assertEqual(frigate.snapshot_calls, [])
        self.assertEqual(
            processor._process_face.call_args.kwargs["source"],
            "secure_live_hires",
        )


if __name__ == "__main__":
    unittest.main()
