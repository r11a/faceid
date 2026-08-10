import tempfile
import time
import unittest
from pathlib import Path

from app.audit import AuditStore
from app.camera_profiles import CameraProfiles
from app.visits import VisitService


class VisitServiceTests(unittest.TestCase):
    def test_groups_repeated_events_and_respects_roles(self):
        with tempfile.TemporaryDirectory() as folder:
            audit = AuditStore(Path(folder) / "audit.db")
            profiles = CameraProfiles(Path(folder) / "profiles.json")
            profiles.update("door", min_face_px=48, role="entry")
            profiles.update("hall", min_face_px=48, role="observation")
            profiles.update("exit", min_face_px=48, role="exit")
            base = time.time() - 5000
            for event_id, camera, timestamp in (
                ("one", "door", base), ("two", "hall", base + 60),
                ("three", "exit", base + 120), ("four", "door", base + 3000),
            ):
                audit.start_event(event_id, camera, timestamp)
                audit.finalize(event_id, "recognized", person="Ronen", score=.8)
            visits = VisitService(audit, profiles, gap_minutes=15).list(
                person="Ronen", days=365, limit=20
            )
            self.assertEqual(len(visits), 2)
            self.assertEqual(visits[-1]["route"], ["door", "hall", "exit"])
            self.assertEqual(visits[-1]["event_count"], 3)
            self.assertEqual(visits[-1]["arrival"], "confirmed")
            self.assertEqual(visits[-1]["departure"], "confirmed")


if __name__ == "__main__":
    unittest.main()
