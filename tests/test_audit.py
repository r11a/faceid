import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path

import numpy as np

from app.audit import AuditStore


class AuditStoreTests(unittest.TestCase):
    def test_native_evidence_replacement_updates_browser_revision(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            audit = AuditStore(Path(tmp) / 'audit.db')
            audit.start_event('faceid-photo', 'door', 100.)
            with patch('app.audit.time.time', return_value=200.):
                audit.save_evidence('faceid-photo', np.zeros((40, 40, 3), dtype=np.uint8))
            first = audit.event_detail('faceid-photo')['event']['updated_ts']
            with patch('app.audit.time.time', return_value=201.):
                audit.save_evidence('faceid-photo', np.full((40, 40, 3), 100, dtype=np.uint8))
            second = audit.event_detail('faceid-photo')['event']['updated_ts']
            self.assertEqual((first, second), (200., 201.))

    def test_first_recognition_time_survives_later_finalization_and_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audit.db"
            audit = AuditStore(path)
            audit.start_event("faceid-late", "door", 100.)
            audit.finalize("faceid-late", "unknown")
            audit.finalize("faceid-late", "recognized", person="Alice", recognition_ts=220.)
            audit.finalize("faceid-late", "recognized", person="Alice", recognition_ts=250.)
            audit.finalize("faceid-late", "recognized", end_ts=300., person="Alice")
            reopened = AuditStore(path)
            metadata = reopened.event_metadata("faceid-late")
            self.assertEqual(metadata["recognition_ts"], 220.)
            self.assertEqual(metadata["end_ts"], 300.)
            self.assertEqual(metadata["start_ts"], 100.)

    def test_recognition_time_migrates_existing_events_without_erasing_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audit.db"
            audit = AuditStore(path)
            audit.start_event("legacy", "door", 100.)
            with closing(sqlite3.connect(path)) as con:
                columns = {row[1] for row in con.execute("PRAGMA table_info(events)")}
                if "recognition_ts" in columns:
                    con.execute("ALTER TABLE events DROP COLUMN recognition_ts")
                con.commit()
            migrated = AuditStore(path)
            metadata = migrated.event_metadata("legacy")
            self.assertEqual(metadata["start_ts"], 100.)
            self.assertIn("recognition_ts", metadata)
            self.assertIsNone(metadata["recognition_ts"])

    def test_media_metadata_reads_event_without_loading_observations(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audit.db"
            audit = AuditStore(path)
            audit.start_event("faceid-local", "door", 100.)
            audit.finalize("faceid-local", "recognized", end_ts=120., person="Alice")
            # An unavailable history table must not prevent serving media ranges.
            with closing(sqlite3.connect(path)) as con:
                con.execute("ALTER TABLE observations RENAME TO unavailable_observations")
                con.commit()
            metadata = audit.event_metadata("faceid-local")
            self.assertEqual(metadata["camera"], "door")
            self.assertEqual(metadata["start_ts"], 100.)
            self.assertEqual(metadata["end_ts"], 120.)
            self.assertEqual(metadata["status"], "recognized")
            self.assertIsNone(audit.event_metadata("missing"))

    def test_liveness_evidence_is_persisted_and_searchable(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = AuditStore(Path(tmp) / "audit.db")
            audit.start_event("live-1", "door", 10.0)
            audit.update_liveness("live-1", {"state": "spoof", "score": .08})
            audit.finalize("live-1", "spoof_suspected", end_ts=11.0)

            event = audit.search_events(
                status="spoof_suspected", limit=10,
            )["events"][0]
            self.assertEqual(event["liveness_status"], "spoof")
            self.assertAlmostEqual(event["liveness_score"], .08)

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

    def test_presence_updates_do_not_inflate_activity_or_statistics(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = AuditStore(Path(tmp) / "audit.db")
            now = time.time()
            for event_id, occurrence in (
                ("arrival", "arrival"),
                ("repeat", "presence_update"),
                ("move", "camera_transition"),
            ):
                audit.start_event(event_id, "front", now)
                audit.finalize(
                    event_id, "recognized", end_ts=now + 1, person="Alice",
                    score=.8, margin=.2, confirmations=2,
                    occurrence=occurrence,
                )

            self.assertEqual(audit.person_statistics()["Alice"]["appearances"], 2)
            self.assertEqual(len(audit.recent()), 2)
            self.assertEqual(audit.search_events()["total"], 2)
            self.assertEqual(
                audit.search_events(include_presence_updates=True)["total"], 3
            )

    def test_evidence_images_are_bounded_without_deleting_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = AuditStore(Path(tmp) / "audit.db")
            image = np.zeros((32, 32, 3), dtype=np.uint8)
            for index in range(5):
                event_id = f"known-{index}"
                audit.start_event(event_id, "front", time.time() + index)
                audit.finalize(event_id, "recognized", person="Alice")
                audit.save_evidence(event_id, image)
            removed = audit.prune_evidence(30, 14, known_max=2, unknown_max=2)
            self.assertEqual(removed, 3)
            self.assertEqual(len(list(audit.evidence_dir.glob("*.jpg"))), 2)
            self.assertEqual(
                audit.search_events(include_presence_updates=True)["total"], 5
            )

    def test_camera_funnel_counts_events_not_observation_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = AuditStore(Path(tmp) / "audit.db")
            now = time.time()
            audit.start_event("event-1", "front", now)
            audit.observation("event-1", 1, "candidate", face_px=40, quality=.3)
            audit.observation("event-1", 2, "recognized", face_px=80, quality=.8)
            audit.finalize(
                "event-1", "recognized", end_ts=now + 1, person="Alice",
                score=.8, margin=.2, confirmations=2,
            )
            funnel = audit.camera_funnels()[0]
            self.assertEqual(funnel["events"], 1)
            self.assertEqual(funnel["face_detected"], 1)
            self.assertEqual(funnel["usable_face"], 1)
            self.assertEqual(funnel["recognized"], 1)

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

    def test_evidence_path_cannot_escape_data_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = AuditStore(Path(tmp) / "audit.db")
            path = audit.evidence_path("../../outside")
            self.assertEqual(path.parent, audit.evidence_dir)
            self.assertEqual(path.suffix, ".jpg")
            self.assertNotIn("outside", path.name)

    def test_filtered_history_profile_and_review_undo(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = AuditStore(Path(tmp) / "audit.db")
            now = time.time()
            audit.start_event("event-1", "front", now)
            audit.finalize(
                "event-1", "recognized", end_ts=now + 1, person="Alice",
                score=0.72, margin=0.2, confirmations=2,
            )
            audit.start_event("event-2", "garage", now - 10)
            audit.finalize(
                "event-2", "unknown", end_ts=now - 9,
                score=0.2, margin=0.01, confirmations=0,
            )
            result = audit.search_events(
                person="Alice", camera="front", date_from=now - 1
            )
            self.assertEqual(result["total"], 1)
            self.assertEqual(result["events"][0]["event_id"], "event-1")
            self.assertTrue(
                audit.set_ground_truth("event-1", "Alice", "tester")
            )
            self.assertEqual(
                audit.event_detail("event-1")["event"]["ground_truth_by"],
                "tester",
            )
            self.assertEqual(audit.undo_ground_truth("event-1", "tester"), "")
            self.assertIsNone(
                audit.event_detail("event-1")["event"]["ground_truth"]
            )
            profile = audit.person_profile("Alice")
            self.assertEqual(profile["statistics"]["appearances"], 1)
            self.assertEqual(profile["events"][0]["camera"], "front")
            report = audit.system_report()
            self.assertEqual({row["camera"] for row in report["cameras"]},
                             {"front", "garage"})

    def test_person_profile_groups_hours_in_requested_timezone(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = AuditStore(Path(tmp) / "audit.db")
            # 2024-01-01 00:30 UTC must stay in hour 00 for a UTC browser,
            # regardless of the timezone configured inside the add-on container.
            timestamp = 1704069000.0
            audit.start_event("event-utc", "front", timestamp)
            audit.finalize(
                "event-utc", "recognized", end_ts=timestamp + 1,
                person="Alice", score=0.8, margin=0.2, confirmations=2,
            )
            profile = audit.person_profile("Alice", timezone_name="UTC")
            hours = {row["hour"]: row["count"] for row in profile["hourly"]}
            self.assertEqual(hours[0], 1)
            self.assertEqual(sum(hours.values()), 1)


if __name__ == "__main__":
    unittest.main()
