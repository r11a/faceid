import tempfile
import asyncio
import unittest
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.webui import build_app
from app.camera_profiles import CameraProfiles
from app.audit import AuditStore
from app.frigate_api import FrigateAPI
from app.media_store import EventMediaStore
from tests.test_media_store import RecordingResponse


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
    def test_native_sighting_stream_probe_prepare_and_range_keep_local_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = AuditStore(Path(tmp) / "audit.db")
            event_id = "faceid-3de2a62c-27be-447e-9044-4b6bc63fe06a"
            audit.start_event(event_id, "front", 100.)
            audit.finalize(event_id, "recognized", end_ts=300., person="Alice", recognition_ts=220.)
            api = FrigateAPI("http://frigate.invalid")
            calls = []
            def request(method, path, **kwargs):
                calls.append((path, kwargs))
                self.assertEqual(path, "/api/front/start/218.000/end/278.000/clip.mp4")
                return RecordingResponse()
            api.request = request
            store = EventMediaStore(Path(tmp), api, metadata_resolver=audit.event_metadata)
            processor = SimpleNamespace(audit=audit, media_store=store, frigate=api)
            app = build_app({"faceid": {}}, SimpleNamespace(), FakeGallery(), processor, Path(tmp), Path(__file__).parents[1] / "static")
            client = TestClient(app)
            url = f"/api/activity/{event_id}/clip"
            streamed = client.get(url, headers={"Range": "bytes=0-", "If-Range": '"recording"'})
            self.assertEqual(streamed.status_code, 200)
            self.assertEqual(streamed.content, RecordingResponse.content)
            self.assertEqual(calls[0][1]["headers"]["If-Range"], '"recording"')
            self.assertIsNone(store.cached_clip_path(event_id))
            probe = client.get(url + "/status?probe=true").json()
            self.assertTrue(probe["ready"])
            self.assertFalse(probe["seekable"])
            self.assertEqual(calls[-1][1]["headers"]["Range"], "bytes=0-0")
            ranged = client.get(url + "?seekable=true", headers={"Range": "bytes=4-7"})
            self.assertEqual(ranged.status_code, 206)
            self.assertEqual(ranged.content, b"ftyp")
            self.assertTrue(client.get(url + "/status?prepare=true").json()["ready"])
            self.assertEqual(len(calls), 3)

    def test_native_sighting_image_uses_evidence_without_synthetic_snapshot_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = AuditStore(Path(tmp) / "audit.db")
            event_id = "faceid-image"
            audit.start_event(event_id, "front", 100.)
            api = FrigateAPI("http://frigate.invalid")
            api.request = lambda *a, **k: self.fail("Native sightings have no Frigate event snapshot")
            processor = SimpleNamespace(audit=audit, media_store=None, frigate=api)
            app = build_app({"faceid": {}}, SimpleNamespace(), FakeGallery(), processor, Path(tmp), Path(__file__).parents[1] / "static")
            client = TestClient(app)
            self.assertEqual(client.get(f"/api/activity/{event_id}/image").status_code, 404)
            saved = audit.save_evidence(event_id, np.full((20, 20, 3), 128, dtype=np.uint8))
            response = client.get(f"/api/activity/{event_id}/image")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, saved.read_bytes())
            self.assertEqual(response.headers['cache-control'], 'private, no-cache')

    def test_camera_profile_api_persists_recognition_settings_and_rejects_invalid_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            profiles = CameraProfiles(Path(tmp) / "camera_profiles.json")
            processor = SimpleNamespace(audit=None, media_store=None,
                                        frigate=SimpleNamespace(cameras=lambda: ["door"], timeout=1),
                                        camera_profiles=profiles, cameras=set(), min_face_px=48)
            app = build_app({"faceid": {"auth": {}}}, SimpleNamespace(), FakeGallery(),
                            processor, Path(tmp), Path(__file__).parents[1] / "static")
            client = TestClient(app)
            payload = {"min_face_px": 80, "role": "entry", "enabled": False,
                       "recognition_mode": "continuous", "recognition_interval_seconds": .75,
                       "recognition_rearm_seconds": 3, "liveness_mode": "required",
                       "require_second_factor": True}
            response = client.post("/api/cameras/door/profile", json=payload)
            self.assertEqual(response.status_code, 200)
            profile = response.json()["profile"]
            self.assertEqual(profile.get("recognition_mode"), "continuous")
            self.assertEqual(profile.get("recognition_interval_seconds"), .75)
            self.assertEqual(profile.get("recognition_rearm_seconds"), 3)
            for key in ("recognition_mode", "recognition_interval_seconds", "recognition_rearm_seconds", "enabled"):
                payload.pop(key)
            updated = client.post("/api/cameras/door/profile", json=payload).json()["profile"]
            self.assertEqual(updated["recognition_mode"], "continuous")
            self.assertEqual(updated["recognition_interval_seconds"], .75)
            self.assertEqual(updated["recognition_rearm_seconds"], 3)
            self.assertFalse(updated["enabled"])
            self.assertEqual(updated["liveness_mode"], "required")
            self.assertTrue(updated["require_second_factor"])
            for bad in ({"recognition_mode": "invalid"}, {"recognition_interval_seconds": .1},
                        {"recognition_rearm_seconds": 31}):
                with self.subTest(bad=bad):
                    response = client.post("/api/cameras/door/profile", json={**payload, **bad})
                    self.assertEqual(response.status_code, 400)

    def test_explicit_seekable_playback_uses_shared_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "clip.mp4"
            path.write_bytes(b"0123456789")
            closed = []
            class Upstream:
                status_code = 200
                headers = {"content-type": "video/mp4"}
                def iter_content(self, chunk_size):
                    self_test.fail("A full progressive stream must not replace a requested byte range")
                    yield b"0123456789"
                def close(self):
                    closed.append(True)
            self_test = self
            processor = SimpleNamespace(
                audit=FakeAudit(),
                media_store=SimpleNamespace(cached_clip_path=lambda _: None, clip_path=lambda _: path),
                frigate=SimpleNamespace(timeout=1, request=lambda *args, **kwargs: Upstream()),
            )
            app = build_app({"faceid": {}}, SimpleNamespace(), FakeGallery(), processor, Path(tmp), Path(__file__).parents[1] / "static")
            response = TestClient(app).get("/api/activity/e1/clip?seekable=true", headers={"Range": "bytes=2-5"})
            self.assertEqual(response.status_code, 206)
            self.assertEqual(response.content, b"2345")
            self.assertEqual(response.headers["content-range"], "bytes 2-5/10")
            self.assertFalse(closed)

    def test_progressive_playback_does_not_wait_for_cache_or_hit_its_size_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            class Upstream:
                status_code = 200
                headers = {"content-type": "video/mp4"}
                def iter_content(self, chunk_size):
                    yield b"progressive-video"
                def close(self):
                    pass
            def unexpected_cache(_):
                self.fail("Normal progressive playback must not download into the bounded cache")
            processor = SimpleNamespace(
                audit=FakeAudit(),
                media_store=SimpleNamespace(cached_clip_path=lambda _: None, clip_path=unexpected_cache),
                frigate=SimpleNamespace(timeout=1, request=lambda *args, **kwargs: Upstream()),
            )
            app = build_app({"faceid": {}}, SimpleNamespace(), FakeGallery(), processor, Path(tmp), Path(__file__).parents[1] / "static")
            response = TestClient(app).get("/api/activity/e1/clip", headers={"Range": "bytes=0-"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, b"progressive-video")
            processor.media_store.clip_path = lambda _: None
            self.assertEqual(TestClient(app).get("/api/activity/e1/clip?seekable=true").status_code, 502)
            self.assertEqual(TestClient(app).get("/api/activity/e1/clip").content, b"progressive-video")

    def test_clip_range_access_does_not_load_observation_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "clip.mp4"
            path.write_bytes(b"video-content")
            def unexpected_detail(_):
                self.fail("Each media range must not load the full observation history")
            processor = SimpleNamespace(
                audit=SimpleNamespace(event_exists=lambda eid: eid == "e1", event_detail=unexpected_detail),
                media_store=FakeMedia(path),
            )
            app = build_app({"faceid": {}}, SimpleNamespace(), FakeGallery(), processor, Path(tmp), Path(__file__).parents[1] / "static")
            client = TestClient(app)
            result = client.get("/api/activity/e1/clip", headers={"Range": "bytes=0-1"})
            self.assertEqual(result.status_code, 206)
            self.assertEqual(result.content, b"vi")
            self.assertEqual(client.get("/api/activity/missing/clip").status_code, 404)

    def test_reference_quality_advice_does_not_disable_recognition(self):
        with tempfile.TemporaryDirectory() as tmp:
            gallery = SimpleNamespace(persons=lambda: {
                "david": {"name": "David", "count": 18, "files": ["face.jpg"]},
                "new": {"name": "New", "count": 0},
                "initial": {"name": "Initial", "count": 1},
            })
            app = build_app({"faceid": {}}, SimpleNamespace(), gallery, SimpleNamespace(audit=None), Path(tmp), Path(__file__).parents[1] / "static")
            with patch("app.webui.gallery_coach_report", return_value={"people": [{"slug":"david", "review_count":3}]}):
                rows = {p["slug"]:p for p in TestClient(app).get("/api/users").json()["users"]}
            self.assertEqual(rows["david"]["state"], "ready")
            self.assertEqual(rows["david"]["state_label"], "פעיל לזיהוי")
            self.assertEqual(rows["david"]["quality_label"], "מומלץ לשפר חלק מהתמונות")
            self.assertEqual(rows["initial"]["state"], "ready")
            self.assertEqual(rows["initial"]["quality_label"], "מומלץ להוסיף זוויות")
            self.assertEqual(rows["new"]["state"], "new")
            self.assertIsNone(rows["new"]["quality_label"])

    def test_dashboard_filters_confirmed_identity_before_pagination(self):
        from app.audit import AuditStore
        with tempfile.TemporaryDirectory() as tmp:
            audit = AuditStore(Path(tmp) / "audit.db")
            for index in range(65):
                audit.start_event(f"other-{index}", "yard", 100 + index)
                audit.finalize(f"other-{index}", "unknown", person="Alice")
            for eid, status, name, ts in [("old", "recognized", "Alice", 1), ("review", "unknown", "Bob", 2), ("rejected", "recognized", "Alice", 200)]:
                audit.start_event(eid, "door", ts)
                audit.finalize(eid, status, person=name)
            audit.set_ground_truth("review", "Alice")
            audit.set_ground_truth("rejected", "__unknown__")
            gallery = SimpleNamespace(persons=lambda: {"alice": {"name": "Alice", "watchlist": "black"}})
            app = build_app({"faceid": {"auth": {}}}, SimpleNamespace(), gallery, SimpleNamespace(audit=audit), Path(tmp), Path(__file__).parents[1] / "static")
            client = TestClient(app)
            result = client.get("/api/dashboard?person=Alice&status=recognized&watchlist=black&camera=door&limit=1").json()
            self.assertEqual([e["event_id"] for e in result["recent"]], ["review"])
            self.assertEqual(result["recent_total"], 2)
            self.assertEqual(result["cameras"], ["door", "yard"])
            self.assertEqual(client.get("/api/dashboard?watchlist=white").json()["recent"], [])
            page = client.get("/api/dashboard?q=Alice&status=recognized&offset=1&limit=1").json()
            self.assertEqual([e["event_id"] for e in page["recent"]], ["old"])
            unknown = client.get("/api/dashboard?status=unknown").json()
            self.assertEqual(unknown["recent_total"], 66)
            self.assertEqual(unknown["recent"][0]["event_id"], "rejected")

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
            self.assertEqual(index.headers["x-faceid-ui-version"], "6.6.1")
            versioned = TestClient(app).get("/ui-previous-release")
            self.assertEqual(versioned.status_code, 200)
            self.assertEqual(versioned.content, index.content)
            ui_asset = TestClient(app).get("/assets/faceid-6.6.1.js")
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
