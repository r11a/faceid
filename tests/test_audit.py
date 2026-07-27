import sqlite3
import tempfile
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


if __name__ == "__main__":
    unittest.main()
