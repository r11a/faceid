import tempfile
import unittest
from pathlib import Path

from app.media_store import EventMediaStore


class FakeFrigate:
    def __init__(self, available=True):
        self.available = available
        self.downloads = 0

    def download_clip(self, event_id, dest, max_bytes):
        self.downloads += 1
        if not self.available:
            return False
        Path(dest).write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"x" * 2048)
        return True

    def event(self, event_id):
        return {
            "id": event_id, "camera": "front", "start_time": 10,
            "end_time": 20, "has_clip": self.available,
        }


class EventMediaStoreTests(unittest.TestCase):
    def test_clip_is_downloaded_once_and_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            frigate = FakeFrigate()
            store = EventMediaStore(Path(tmp), frigate)
            first = store.clip_path("event/unsafe-looking")
            second = store.clip_path("event/unsafe-looking")
            self.assertEqual(first, second)
            self.assertTrue(first.is_file())
            self.assertEqual(frigate.downloads, 1)
            self.assertEqual(first.parent, Path(tmp) / "media_cache")
            self.assertEqual(store._locks, {})

    def test_failed_or_non_mp4_download_is_not_cached(self):
        with tempfile.TemporaryDirectory() as tmp:
            frigate = FakeFrigate(available=False)
            store = EventMediaStore(Path(tmp), frigate)
            self.assertIsNone(store.clip_path("missing"))
            self.assertEqual(list((Path(tmp) / "media_cache").iterdir()), [])
            self.assertEqual(store._locks, {})

    def test_status_distinguishes_event_clip_and_recording_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            frigate = FakeFrigate(available=False)
            store = EventMediaStore(Path(tmp), frigate)
            status = store.status("e1")
            self.assertFalse(status["has_clip"])
            self.assertTrue(status["has_recording_window"])


if __name__ == "__main__":
    unittest.main()
