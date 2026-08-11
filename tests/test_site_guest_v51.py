import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

from app.camera_profiles import CameraProfiles
from app.guest_access import GuestAccess
from app.site_intelligence import SiteIntelligence


class _Audit:
    def traffic_events(self, after_ts, limit=10000):
        return [
            {"camera": "door", "start_ts": time.time(), "status": "recognized"},
            {"camera": "hall", "start_ts": time.time(), "status": "no_face"},
        ]


class _Visits:
    def list(self, **_kwargs):
        return [{"route": ["door", "hall"], "person": "Alice"}]


class GuestAndSiteTests(unittest.TestCase):
    def test_guest_is_scoped_and_fails_closed_without_second_factor(self):
        with tempfile.TemporaryDirectory() as folder:
            service = GuestAccess(Path(folder), threshold=.62, margin=.12)
            embedding = np.zeros(512, dtype=np.float32); embedding[0] = 1
            crop = np.zeros((120, 120, 3), dtype=np.uint8)
            guest = service.create(
                name="Visitor", valid_from=time.time() - 10,
                valid_until=time.time() + 3600, max_entries=1,
                allowed_cameras=["door"], crop=crop, embedding=embedding,
            )
            self.assertEqual(len(service.candidates(embedding, camera="door")), 1)
            self.assertEqual(service.candidates(embedding, camera="office"), [])
            pending = service.evaluate(
                guest["id"], camera="door", score=.9, runner_up_score=.2,
                liveness_confirmed=True, second_factor=False,
            )
            self.assertFalse(pending["authorized"])
            self.assertIn("second_factor_required", pending["reasons"])
            approved = service.evaluate(
                guest["id"], camera="door", score=.9, runner_up_score=.2,
                liveness_confirmed=True, second_factor=True,
            )
            self.assertTrue(approved["authorized"])
            self.assertEqual(approved["entries_left"], 0)
            self.assertEqual(service.list()[0]["status"], "used")

    def test_site_map_persists_positions_and_returns_anonymous_counts(self):
        with tempfile.TemporaryDirectory() as folder:
            profiles = CameraProfiles(Path(folder) / "cameras.json")
            service = SiteIntelligence(
                Path(folder) / "map.json", _Audit(), profiles, _Visits()
            )
            result = service.update({
                "title": "Office", "cameras": [
                    {"camera": "door", "x": 20, "y": 30},
                    {"camera": "hall", "x": 70, "y": 60},
                ], "links": [["door", "hall"]],
            }, ["door", "hall"])
            self.assertEqual(result["links"], [["door", "hall"]])
            analytics = service.analytics(cameras=["door", "hall"], days=7)
            self.assertEqual(analytics["total_person_events"], 2)
            self.assertNotIn("person", analytics)
            self.assertEqual(analytics["transitions"][0]["count"], 1)

    def test_ui_contains_route_playlist_and_product_tabs(self):
        html = (Path(__file__).parents[1] / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("data-t=\"guests\"", html)
        self.assertIn("data-t=\"site-map\"", html)
        self.assertIn("showRoutePlaylist", html)
        self.assertIn("goToRouteClip", html)
        self.assertIn("לחצו כדי לעבור לקליפ הזה", html)
        self.assertIn("נגן קליפים לפי מסלול", html)


if __name__ == "__main__":
    unittest.main()
