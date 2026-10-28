import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.media_store import EventMediaStore
from app.frigate_api import FrigateAPI


class RecordingResponse:
    status_code = 200
    headers = {"content-type": "video/mp4"}
    content = b"\x00\x00\x00\x18ftypmp42" + b"x" * 2048

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def iter_content(self, chunk_size):
        yield self.content

    def close(self):
        pass


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
    def test_late_recognition_replaces_early_cached_window_with_identity_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            api = FrigateAPI("http://frigate.invalid")
            calls = []
            def request(method, path, **kwargs):
                calls.append(path)
                return RecordingResponse()
            api.request = request
            metadata = {"camera": "door", "start_ts": 100., "end_ts": None}
            store = EventMediaStore(Path(tmp), api, metadata_resolver=lambda _: metadata)
            with patch("app.frigate_api.time.time", return_value=1000.):
                early = store.clip_path("faceid-late")
                metadata.update(recognition_ts=220., end_ts=300.)
                self.assertIsNone(store.cached_clip_path("faceid-late"))
                recognized = store.clip_path("faceid-late")
                self.assertNotEqual(recognized, early)
                self.assertEqual(recognized.read_bytes(), RecordingResponse.content)
                self.assertEqual(calls, [
                    "/api/door/start/98.000/end/162.000/clip.mp4",
                    "/api/door/start/218.000/end/278.000/clip.mp4",
                ])

    def test_native_sighting_cache_and_status_use_only_local_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            api = FrigateAPI("http://frigate.invalid")
            calls = []
            def request(method, path, **kwargs):
                calls.append(path)
                self.assertFalse(path.startswith("/api/events/"))
                return RecordingResponse()
            api.request = request
            store = EventMediaStore(
                Path(tmp), api, metadata_resolver=lambda _: {
                    "camera": "front door", "start_ts": 100., "end_ts": 120.,
                },
            )
            status = store.status("faceid-test")
            self.assertFalse(status["has_clip"])
            self.assertTrue(status["has_recording_window"])
            self.assertEqual(status["camera"], "front door")
            self.assertEqual(calls, [])
            path = store.clip_path("faceid-test")
            self.assertEqual(path.read_bytes(), RecordingResponse.content)
            self.assertEqual(store.clip_path("faceid-test"), path)
            self.assertEqual(calls, ["/api/front%20door/start/98.000/end/122.000/clip.mp4"])

    def test_native_sighting_without_local_metadata_never_downloads_event_clip(self):
        with tempfile.TemporaryDirectory() as tmp:
            api = FrigateAPI("http://frigate.invalid")
            api.request = lambda *a, **k: self.fail("Synthetic IDs must never reach Frigate events")
            store = EventMediaStore(Path(tmp), api)
            self.assertIsNone(store.clip_path("faceid-missing"))
            self.assertFalse(store.status("faceid-missing")["has_recording_window"])

    def test_native_recording_window_rejects_invalid_times_and_bounds_ongoing_tracks(self):
        with tempfile.TemporaryDirectory() as tmp:
            api = FrigateAPI("http://frigate.invalid")
            calls = []
            def request(method, path, **kwargs):
                calls.append(path)
                return RecordingResponse()
            api.request = request
            dest = str(Path(tmp) / "clip.mp4")
            with patch("app.frigate_api.time.time", return_value=1000.):
                self.assertTrue(api.download_clip("faceid-active", dest, metadata={
                    "camera": "door", "start_ts": 100., "end_ts": None,
                }))
                self.assertEqual(calls, ["/api/door/start/98.000/end/162.000/clip.mp4"])
                calls.clear()
                self.assertTrue(api.download_clip("faceid-recent", dest, metadata={
                    "camera": "door", "start_ts": 995., "end_ts": None,
                }))
                self.assertEqual(calls, ["/api/door/start/993.000/end/1000.000/clip.mp4"])
                calls.clear()
                for metadata in [None, {}, {"camera": "door", "start_ts": "bad"},
                                 {"camera": "door", "start_ts": float("nan")},
                                 {"camera": "door", "start_ts": 2000.},
                                 {"camera": "door", "start_ts": 100., "end_ts": 99.}]:
                    self.assertFalse(api.download_clip("faceid-invalid", dest, metadata=metadata))
                self.assertEqual(calls, [])

    def test_cached_path_never_downloads_on_cache_miss(self):
        with tempfile.TemporaryDirectory() as tmp:
            frigate = FakeFrigate()
            store = EventMediaStore(Path(tmp), frigate)
            self.assertIsNone(store.cached_clip_path("e1"))
            self.assertEqual(frigate.downloads, 0)
            stored = store.clip_path("e1")
            self.assertEqual(store.cached_clip_path("e1"), stored)
            self.assertEqual(frigate.downloads, 1)

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
