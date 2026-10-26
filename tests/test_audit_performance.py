import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from app.audit import AuditStore
from app.camera_profiles import CameraProfiles
from app.visits import VisitService


class AuditPerformanceTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.audit = AuditStore(Path(self.folder.name) / "audit.db")
        self.now = time.time()

    def event(self, event_id, person, camera="door", age=0, occurrence=None):
        self.audit.start_event(event_id, camera, self.now - age)
        self.audit.finalize(event_id, "recognized", person=person, score=.8,
                            occurrence=occurrence)

    def trace(self):
        statements = []
        connect = self.audit._connect

        def traced_connect():
            con = connect()
            con.set_trace_callback(statements.append)
            return con

        return statements, patch.object(self.audit, "_connect", traced_connect)

    def test_statistics_query_count_does_not_grow_with_people(self):
        for index in range(20):
            self.event(f"event-{index}", f"Person-{index}")
        statements, tracing = self.trace()
        with tracing:
            stats = self.audit.person_statistics()
        self.assertEqual(len(stats), 20)
        reads = [sql for sql in statements if sql.lstrip().upper().startswith(("SELECT", "WITH"))]
        self.assertLessEqual(len(reads), 3)

    def test_statistics_preserve_reviews_camera_order_and_latest_presence(self):
        self.event("first", "Alice", "door", age=300)
        self.event("reviewed", "Bob", "hall", age=200)
        self.audit.set_ground_truth("reviewed", "Alice")
        self.event("rejected", "Alice", age=100)
        self.audit.set_ground_truth("rejected", "__unknown__")
        self.event("latest", "Alice", "yard", occurrence="presence_update")
        self.event("quiet-only", "Quiet", occurrence="presence_update")
        stats = self.audit.person_statistics()
        self.assertEqual(set(stats), {"Alice"})
        alice = stats["Alice"]
        self.assertEqual(alice["appearances"], 2)
        self.assertEqual(alice["last_7_days"], 2)
        self.assertEqual(alice["last_30_days"], 2)
        self.assertEqual(alice["avg_score"], .8)
        self.assertEqual(alice["last_event_id"], "latest")
        self.assertEqual(alice["last_camera"], "yard")
        self.assertEqual(alice["last_seen"], self.now)
        self.assertEqual(alice["cameras"], [{"camera": "door", "count": 1}, {"camera": "hall", "count": 1}])
        self.assertEqual(alice["top_camera"], "door")

    def test_statistics_use_one_snapshot_during_concurrent_review(self):
        self.event("event", "Alice")
        connect = self.audit._connect
        reviewed = []

        def review_between_reads(sql):
            if "ROW_NUMBER()" in sql and not reviewed:
                with closing(sqlite3.connect(self.audit.path)) as writer:
                    with writer:
                        writer.execute("UPDATE events SET ground_truth='Bob' WHERE event_id='event'")
                reviewed.append(True)

        def traced_connect():
            con = connect()
            con.set_trace_callback(review_between_reads)
            return con

        with patch.object(self.audit, "_connect", traced_connect):
            stats = self.audit.person_statistics()
        self.assertEqual(reviewed, [True])
        self.assertEqual(stats["Alice"]["last_event_id"], "event")
        self.assertEqual(stats["Alice"]["cameras"], [{"camera": "door", "count": 1}])
        self.assertEqual(set(self.audit.person_statistics()), {"Bob"})

    def test_event_exists_reads_no_observations_and_tracks_deletion(self):
        self.event("exists", "Alice")
        self.audit.observation("exists", 1, "recognized", person="Alice")
        statements, tracing = self.trace()
        with tracing:
            self.assertTrue(self.audit.event_exists("exists"))
            self.assertFalse(self.audit.event_exists("missing"))
        self.assertFalse(any("observations" in sql.lower() for sql in statements))
        self.audit.delete_person_history("Alice")
        self.assertFalse(self.audit.event_exists("exists"))

    def test_visit_roles_are_read_once_per_camera_per_list(self):
        for index in range(10):
            self.event(f"event-{index}", "Alice", age=100-index)
        profiles = CameraProfiles(Path(self.folder.name) / "profiles.json")
        reads = []
        get = profiles.get

        def counting_get(camera):
            reads.append(camera)
            return get(camera)

        with patch.object(profiles, "get", counting_get):
            visits = VisitService(self.audit, profiles).list()
        self.assertEqual(visits[0]["event_count"], 10)
        self.assertEqual(reads, ["door"])
        profiles.update("door", min_face_px=48, role="exit")
        visits = VisitService(self.audit, profiles).list()
        self.assertEqual(len(visits), 10)
        self.assertTrue(all(visit["departure"] == "confirmed" for visit in visits))

    def test_bulk_visit_statistics_preserve_per_person_limit(self):
        # More than 1,000 visits overall must not hide another person's visits.
        with self.audit._connection() as con:
            con.executemany(
                "INSERT INTO events(event_id, camera, start_ts, status, person, score, created_ts, updated_ts) "
                "VALUES(?, 'door', ?, 'recognized', ?, .8, ?, ?)",
                [(f"event-{index}", self.now-(1002-index)*1000,
                  "Alice" if index < 1001 else "Bob", self.now, self.now)
                 for index in range(1002)],
            )
        profiles = CameraProfiles(Path(self.folder.name) / "profiles.json")
        service = VisitService(self.audit, profiles)
        expected = {name: service.person_statistics(name) for name in ("Alice", "Bob", "Missing")}
        statements, tracing = self.trace()
        with tracing:
            actual = service.person_statistics_many(["Alice", "Bob", "Missing"])
        self.assertEqual(actual, expected)
        self.assertEqual(actual["Alice"]["visits"], 1000)
        reads = [sql for sql in statements if sql.lstrip().upper().startswith("SELECT")]
        self.assertEqual(len(reads), 1)


if __name__ == "__main__":
    unittest.main()
