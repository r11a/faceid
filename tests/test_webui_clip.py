import tempfile
import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.webui import build_app
from app.camera_profiles import CameraProfiles


class FakeAudit:
    def event_detail(self, event_id):
        return {"event": {"event_id": event_id}} if event_id == "e1" else None


class FakeMedia:
    def __init__(self, path):
        self.path = path

    def clip_path(self, event_id):
        return self.path if event_id == "e1" else None

    def cached_clip_path(self, event_id):
        return self.clip_path(event_id)

    def status(self, event_id):
        return {"cached": True, "has_clip": True}


class FakeGallery:
    def persons(self):
        return {}

    def match_candidates(self, _embedding, limit=2):
        return [("ronen", "Ronen", .81)][:limit]


class WebUIClipTests(unittest.TestCase):
    def test_uncached_clip_streams_range_without_downloading_full_clip(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls, closed = [], []
            class Upstream:
                status_code = 206
                headers = {"content-range": "bytes 4-7/100", "content-length": "4", "accept-ranges": "bytes", "content-type": "video/mp4"}
                def iter_content(self, chunk_size):
                    yield b"part"
                def close(self):
                    closed.append(True)
            def request(method, path, **kwargs):
                calls.append((method, path, kwargs))
                return Upstream()
            def unexpected_download(*args, **kwargs):
                self.fail("playback must not wait for a complete cache download")
            processor = SimpleNamespace(
                audit=FakeAudit(),
                media_store=SimpleNamespace(cached_clip_path=lambda _: None, clip_path=unexpected_download),
                frigate=SimpleNamespace(timeout=1, request=request),
            )
            app = build_app({"faceid": {"auth": {}}}, SimpleNamespace(), FakeGallery(), processor, Path(tmp), Path(__file__).parents[1] / "static")
            response = TestClient(app).get("/api/activity/e1/clip", headers={"Range": "bytes=4-7", "If-Range": '"clip-version"'})
            self.assertEqual(response.status_code, 206)
            self.assertEqual(response.content, b"part")
            self.assertEqual(response.headers["content-range"], "bytes 4-7/100")
            self.assertEqual(calls[0][2]["headers"]["Range"], "bytes=4-7")
            self.assertEqual(calls[0][2]["headers"]["If-Range"], '"clip-version"')
            self.assertTrue(calls[0][2]["stream"])
            self.assertTrue(closed)

            # Response construction must not read the body: first bytes can be
            # sent before the second upstream chunk is requested.
            reads = []
            def chunks(chunk_size):
                reads.append("first")
                yield b"part"
                reads.append("second")
                yield b"rest"
            Upstream.iter_content = lambda self, chunk_size: chunks(chunk_size)
            route = next(route for route in app.routes if route.path == "/api/activity/{event_id}/clip")
            response = route.endpoint("e1", Request({"type": "http", "headers": []}))
            self.assertEqual(reads, [])
            async def first_chunk():
                self.assertEqual(await anext(response.body_iterator), b"part")
                self.assertEqual(reads, ["first"])
                await response.body_iterator.aclose()
                await response.background()
            asyncio.run(first_chunk())

    def test_upstream_unavailable_and_unsatisfiable_ranges_close_connection(self):
        with tempfile.TemporaryDirectory() as tmp:
            class Upstream:
                status_code = 416
                headers = {"content-range": "bytes */100"}
                closed = False
                def close(self):
                    self.closed = True
            upstream = Upstream()
            processor = SimpleNamespace(audit=FakeAudit(), media_store=None, frigate=SimpleNamespace(timeout=1, request=lambda *a, **k: upstream))
            app = build_app({"faceid": {"auth": {}}}, SimpleNamespace(), FakeGallery(), processor, Path(tmp), Path(__file__).parents[1] / "static")
            response = TestClient(app).get("/api/activity/e1/clip", headers={"Range": "bytes=1000-"})
            self.assertEqual(response.status_code, 416)
            self.assertEqual(response.headers["content-range"], "bytes */100")
            self.assertTrue(upstream.closed)
            upstream.status_code = 404
            upstream.closed = False
            response = TestClient(app).get("/api/activity/e1/clip")
            self.assertEqual(response.status_code, 404)
            self.assertIn("not available yet", response.json()["detail"])
            self.assertTrue(upstream.closed)

    def test_recording_fallback_streams_only_the_finished_event_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            class Upstream:
                headers = {"content-type": "video/mp4"}
                def __init__(self, status):
                    self.status_code = status
                def close(self):
                    pass
                def iter_content(self, chunk_size):
                    yield b"event-video"
            def request(method, path, **kwargs):
                calls.append(path)
                return Upstream(404 if path.startswith("/api/events/") else 200)
            metadata = {"camera": "front", "start_time": 100., "end_time": 120.}
            processor = SimpleNamespace(audit=FakeAudit(), media_store=None, frigate=SimpleNamespace(timeout=1, request=request, event=lambda _: metadata))
            app = build_app({"faceid": {"auth": {}}}, SimpleNamespace(), FakeGallery(), processor, Path(tmp), Path(__file__).parents[1] / "static")
            response = TestClient(app).get("/api/activity/e1/clip")
            self.assertEqual(response.content, b"event-video")
            self.assertEqual(calls, ["/api/events/e1/clip.mp4", "/api/front/start/98.000/end/122.000/clip.mp4"])
            calls.clear()
            metadata["end_time"] = None
            self.assertEqual(TestClient(app).get("/api/activity/e1/clip").status_code, 404)
            self.assertEqual(calls, ["/api/events/e1/clip.mp4"])

    def test_camera_selection_defaults_on_and_persists_ui_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            profiles = CameraProfiles(Path(tmp) / "camera_profiles.json")
            frigate = SimpleNamespace(cameras=lambda: ["door"], timeout=1)
            processor = SimpleNamespace(
                audit=None, media_store=None, frigate=frigate,
                camera_profiles=profiles, cameras=set(), min_face_px=48,
            )

            def camera_enabled(camera):
                stored = profiles.all().get(camera)
                return bool(stored.get("enabled", True)) if stored else True

            processor.camera_enabled = camera_enabled
            processor.set_camera_enabled = profiles.set_enabled
            app = build_app(
                {"faceid": {"auth": {}}}, SimpleNamespace(), FakeGallery(),
                processor, Path(tmp), Path(__file__).parents[1] / "static",
            )
            client = TestClient(app)
            initial = client.get("/api/cameras/studio").json()["cameras"]
            self.assertTrue(initial[0]["enabled"])
            response = client.post(
                "/api/cameras/door/enabled", json={"enabled": False}
            )
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json()["enabled"])
            self.assertFalse(
                client.get("/api/cameras/studio").json()["cameras"][0]["enabled"]
            )

    def test_liveness_capture_returns_exact_temporary_preview_and_guidance(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = np.full((240, 320, 3), 128, dtype=np.uint8)
            ok, encoded = cv2.imencode(".jpg", image)
            self.assertTrue(ok)
            profile = {
                "camera": "door", "min_face_px": 80,
                "night_min_face_px": 80, "role": "intercom",
                "mode": "intercom", "burst_frames": 3,
                "high_resolution": True, "require_second_factor": True,
                "liveness_mode": "required", "roi": [0, 0, 1, 1],
            }
            profiles = SimpleNamespace(
                all=lambda: {"door": profile}, get=lambda _camera: profile,
            )
            frigate = SimpleNamespace(
                cameras=lambda: ["door"],
                latest_frame_bytes=lambda _camera: encoded.tobytes(),
            )
            liveness = SimpleNamespace(
                analyze=lambda _image, _face: {
                    "state": "live", "live": True, "score": .92,
                },
                consensus=lambda _history: {
                    "state": "live", "confirmed": True, "live_frames": 3,
                    "required_frames": 3, "score": .92,
                },
            )
            face = SimpleNamespace(
                bbox=np.asarray([100, 60, 200, 160]), det_score=.96,
                kps=np.asarray([[125, 95], [175, 95], [150, 120]]),
                normed_embedding=np.zeros(4, dtype=np.float32),
            )
            processor = SimpleNamespace(
                audit=None, media_store=None, frigate=frigate,
                camera_profiles=profiles, cameras={"door"},
                min_face_quality=.35, liveness=liveness,
            )
            app = build_app(
                {"faceid": {"auth": {}}},
                SimpleNamespace(faces=lambda _image: [face]), FakeGallery(),
                processor, Path(tmp), Path(__file__).parents[1] / "static",
            )
            client = TestClient(app)
            result = client.post("/api/intercom/door/capture")
            self.assertEqual(result.status_code, 200)
            payload = result.json()
            self.assertEqual(payload["best"]["face_px"], 100)
            self.assertEqual(payload["liveness"]["state"], "live")
            self.assertTrue(payload["guidance"])
            preview = client.get("/api/intercom/door/capture/preview")
            self.assertEqual(preview.status_code, 200)
            self.assertEqual(preview.headers["content-type"], "image/jpeg")
            self.assertIn("no-store", preview.headers["cache-control"])

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
            self.assertIn("no-store", index.headers["cache-control"])
            self.assertNotIn("etag", index.headers)
            self.assertNotIn("last-modified", index.headers)
            self.assertEqual(index.headers["x-faceid-ui-version"], "6.4.0")
            versioned = TestClient(app).get("/ui-previous-release")
            self.assertEqual(versioned.status_code, 200)
            self.assertEqual(versioned.content, index.content)
            ui_asset = TestClient(app).get("/assets/faceid-6.4.0.js")
            self.assertEqual(ui_asset.status_code, 200)
            self.assertIn("immutable", ui_asset.headers["cache-control"])
            self.assertEqual(ui_asset.headers["x-content-type-options"], "nosniff")
            response = TestClient(app).get(
                "/api/activity/e1/clip", headers={"Range": "bytes=0-15"}
            )
            self.assertEqual(response.status_code, 206)
            self.assertEqual(response.content, data[:16])
            self.assertEqual(response.headers["accept-ranges"], "bytes")


if __name__ == "__main__":
    unittest.main()
