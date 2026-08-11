import unittest

from app.backfill import run_backfill


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeFrigate:
    def __init__(self):
        self.snapshots = []

    def request(self, *_args, **_kwargs):
        return FakeResponse([
            {"id": "front-1", "camera": "front", "start_time": 10},
            {"id": "garden-1", "camera": "garden", "start_time": 9},
        ])

    def snapshot(self, event_id, crop=True):
        self.snapshots.append((event_id, crop))
        return None


class BackfillCameraSelectionTests(unittest.TestCase):
    def test_history_scan_skips_disabled_cameras_before_fetching_images(self):
        frigate = FakeFrigate()
        stats = run_backfill(
            engine=None, gallery=None, frigate=frigate, frigate_url="http://frigate",
            camera_enabled=lambda camera: camera == "front",
        )
        self.assertEqual(stats["events"], 1)
        self.assertEqual(stats["no_face"], 1)
        self.assertEqual(frigate.snapshots, [("front-1", True)])


if __name__ == "__main__":
    unittest.main()
