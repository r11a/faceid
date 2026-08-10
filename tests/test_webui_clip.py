import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.webui import build_app


class FakeAudit:
    def event_detail(self, event_id):
        return {"event": {"event_id": event_id}} if event_id == "e1" else None


class FakeMedia:
    def __init__(self, path):
        self.path = path

    def clip_path(self, event_id):
        return self.path if event_id == "e1" else None

    def status(self, event_id):
        return {"cached": True, "has_clip": True}


class FakeGallery:
    def persons(self):
        return {}


class WebUIClipTests(unittest.TestCase):
    def test_cached_clip_supports_browser_byte_ranges(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "clip.mp4"
            data = b"\x00\x00\x00\x18ftypmp42" + b"v" * 4096
            path.write_bytes(data)
            processor = SimpleNamespace(
                audit=FakeAudit(), media_store=FakeMedia(path),
                frigate=SimpleNamespace(timeout=1),
            )
            app = build_app(
                {"faceid": {"auth": {}}}, SimpleNamespace(), FakeGallery(),
                processor, Path(tmp), Path(__file__).parents[1] / "static",
            )
            index = TestClient(app).get("/")
            self.assertEqual(index.headers["cache-control"], "no-store, max-age=0")
            response = TestClient(app).get(
                "/api/activity/e1/clip", headers={"Range": "bytes=0-15"}
            )
            self.assertEqual(response.status_code, 206)
            self.assertEqual(response.content, data[:16])
            self.assertEqual(response.headers["accept-ranges"], "bytes")


if __name__ == "__main__":
    unittest.main()
