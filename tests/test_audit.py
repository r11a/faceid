import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from app.audit import AuditStore


class AuditStoreTests(unittest.TestCase):
    def test_event_lifecycle_is_persistent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audit.db"
            audit = AuditStore(path)
            audit.start_event("event-1", "front", 100.0)
            self.assertFalse(audit.was_finalized("event-1"))
            audit.observation(
                "event-1", 1, "recognized_candidate", person="Alice",
                score=0.61, runner_up="Bob", runner_up_score=0.22,
                margin=0.39, det_score=0.95, face_px=96,
            )
            audit.finalize(
                "event-1", "recognized", end_ts=110.0, person="Alice",
                score=0.61, margin=0.39, confirmations=2,
            )
            self.assertTrue(audit.was_finalized("event-1"))
            recent = audit.recent()
            self.assertEqual(recent[0]["event_id"], "event-1")
            self.assertEqual(recent[0]["status"], "recognized")
            detail = audit.event_detail("event-1")
            self.assertEqual(detail["observations"][0]["runner_up"], "Bob")

            con = sqlite3.connect(path)
            try:
                event = con.execute(
                    "SELECT status, person, confirmations FROM events WHERE event_id=?",
                    ("event-1",),
                ).fetchone()
                observation = con.execute(
                    "SELECT status, runner_up, face_px FROM observations WHERE event_id=?",
                    ("event-1",),
                ).fetchone()
            finally:
                con.close()
            self.assertEqual(event, ("recognized", "Alice", 2))
            self.assertEqual(observation, ("recognized_candidate", "Bob", 96))

    def test_person_statistics_and_dashboard_count_final_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = AuditStore(Path(tmp) / "audit.db")
            now = time.time()
            for event_id, camera, score in (
                ("event-1", "front", 0.80),
                ("event-2", "front", 0.60),
                ("event-3", "garage", 0.70),
            ):
                audit.start_event(event_id, camera, now)
                audit.finalize(
                    event_id, "recognized", end_ts=now + 1,
                    person="Alice", score=score, margin=0.2, confirmations=2,
                )
            stats = audit.person_statistics()["Alice"]
            self.assertEqual(stats["appearances"], 3)
            self.assertEqual(stats["today"], 3)
            self.assertEqual(stats["top_camera"], "front")
            self.assertAlmostEqual(stats["avg_score"], 0.7)
            summary = audit.dashboard_summary()
            self.assertEqual(summary["recognized_24h"], 3)

    def test_processing_event_cannot_be_labeled(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = AuditStore(Path(tmp) / "audit.db")
            audit.start_event("event-1", "front", time.time())
            self.assertFalse(audit.set_ground_truth("event-1", "Alice"))
            audit.finalize(
                "event-1", "unknown", end_ts=time.time(),
                score=0, margin=0, confirmations=0,
            )
            self.assertTrue(audit.set_ground_truth("event-1", "Alice"))


if __name__ == "__main__":
    unittest.main()
