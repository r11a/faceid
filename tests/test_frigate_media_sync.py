import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

from app.frigate_api import FrigateAPI
from app.frigate_sync import FrigateGallerySync
from app.gallery_coach import gallery_coach_report


class FakeResponse:
    def __init__(self, status_code=200, *, payload=None, content=b"", text=""):
        self.status_code = status_code
        self._payload = payload
        self.content = content
        self.text = text

    def json(self):
        return self._payload

    def iter_content(self, chunk_size):
        yield self.content

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class RecordingFallbackFrigate(FrigateAPI):
    def __init__(self):
        self.base = "https://frigate.test:8971"
        self.timeout = 1
        self.calls = []

    def request(self, method, path, **kwargs):
        self.calls.append(path)
        if path == "/api/events/event-1/clip.mp4":
            return FakeResponse(404)
        if path == "/api/events/event-1":
            return FakeResponse(payload={
                "camera": "front door", "start_time": 10.0,
                "end_time": 20.0, "has_clip": False,
            })
        if path == "/api/front%20door/start/8.000/end/22.000/clip.mp4":
            return FakeResponse(content=b"x" * 2048)
        return FakeResponse(404)


class FakeGallery:
    def __init__(self, root):
        self.persons_dir = Path(root) / "persons"
        self.persons_dir.mkdir()
        pdir = self.persons_dir / "alice"
        pdir.mkdir()
        image = np.full((96, 96, 3), 128, dtype=np.uint8)
        cv2.putText(image, "A", (20, 70), cv2.FONT_HERSHEY_SIMPLEX, 2, (255, 255, 255), 3)
        cv2.imwrite(str(pdir / "one.jpg"), image)
        np.save(pdir / "embeddings.npy", np.ones((1, 512), dtype=np.float32) / np.sqrt(512))

    def persons(self):
        return {"alice": {"name": "Alice", "files": ["one.jpg"], "sources": {}}}


class FakeSyncFrigate:
    timeout = 1

    def __init__(self):
        self.calls = []

    def request(self, method, path, **kwargs):
        self.calls.append((method, path))
        if method == "GET" and path == "/api/faces":
            return FakeResponse(payload={})
        return FakeResponse(200, text="ok")


class FrigateMediaAndSyncTests(unittest.TestCase):
    def test_clip_falls_back_to_camera_recording_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            frigate = RecordingFallbackFrigate()
            dest = Path(tmp) / "clip.mp4"
            self.assertTrue(frigate.download_clip("event-1", str(dest)))
            self.assertEqual(dest.stat().st_size, 2048)
            self.assertIn(
                "/api/front%20door/start/8.000/end/22.000/clip.mp4",
                frigate.calls,
            )

    def test_explicit_export_is_ledgered_and_not_repeated(self):
        with tempfile.TemporaryDirectory() as tmp:
            gallery = FakeGallery(tmp)
            frigate = FakeSyncFrigate()
            sync = FrigateGallerySync(Path(tmp), gallery, object(), frigate)
            item = {"slug": "alice", "file": "one.jpg"}
            first = sync.export_selected([item])
            second = sync.export_selected([item])
            self.assertEqual(first["exported"], 1)
            self.assertEqual(second["skipped"], 1)
            self.assertEqual(
                sum(path.endswith("/register") for _, path in frigate.calls), 1
            )

    def test_gallery_coach_is_explainable_and_non_mutating(self):
        with tempfile.TemporaryDirectory() as tmp:
            gallery = FakeGallery(tmp)
            report = gallery_coach_report(gallery)
            self.assertEqual(report["summary"]["images"], 1)
            row = report["people"][0]["images"][0]
            self.assertIn("sharpness", row)
            self.assertTrue((gallery.persons_dir / "alice" / "one.jpg").is_file())


if __name__ == "__main__":
    unittest.main()
