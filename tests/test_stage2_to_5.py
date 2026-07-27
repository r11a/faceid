import json
import tempfile
import time
import unittest
from pathlib import Path

from app.audit import AuditStore
from app.integrations import IntegrationDispatcher
from app.scenarios import ScenarioManager


class FakeClient:
    def __init__(self):
        self.messages = []

    def publish(self, topic, payload, **kwargs):
        self.messages.append((topic, json.loads(payload)))


class StageTwoToFiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.audit = AuditStore(Path(self.tmp.name) / "audit.db", retention_days=0)

    def tearDown(self):
        self.tmp.cleanup()

    def test_jobs_survive_running_recovery(self):
        self.audit.start_event("e1", "front", 1)
        self.audit.mark_ended("e1", 2)
        self.audit.queue_job("e1", "clip")
        self.audit.mark_job_running("e1", "clip")
        self.audit.recover_running_jobs()
        jobs = self.audit.pending_jobs()
        self.assertEqual([(j["event_id"], j["kind"], j["end_ts"]) for j in jobs],
                         [("e1", "clip", 2.0)])

    def test_scenarios_require_identity_link_for_unknowns(self):
        manager = ScenarioManager(
            self.audit, window_seconds=90,
            camera_graph={"front": ["hall"], "hall": ["front"]},
        )
        for event_id, camera, start in (("e1", "front", 10), ("e2", "hall", 20)):
            self.audit.start_event(event_id, camera, start)
        first = manager.attach(
            "e1", camera="front", start_ts=10, end_ts=12,
            status="unknown", person=None,
        )
        second = manager.attach(
            "e2", camera="hall", start_ts=20, end_ts=22,
            status="unknown", person=None,
        )
        self.assertNotEqual(first["scenario_id"], second["scenario_id"])

    def test_recognized_person_continues_across_adjacent_cameras(self):
        manager = ScenarioManager(
            self.audit, window_seconds=90,
            camera_graph={"front": ["hall"], "hall": ["front"]},
        )
        for event_id, camera, start in (("e1", "front", 10), ("e2", "hall", 20)):
            self.audit.start_event(event_id, camera, start)
        first = manager.attach(
            "e1", camera="front", start_ts=10, end_ts=12,
            status="recognized", person="Alice",
        )
        second = manager.attach(
            "e2", camera="hall", start_ts=20, end_ts=22,
            status="recognized", person="Alice",
        )
        self.assertEqual(first["scenario_id"], second["scenario_id"])
        self.assertEqual(second["event_count"], 2)

    def test_versioned_automation_event_is_deduplicated(self):
        dispatcher = IntegrationDispatcher(cooldown_seconds=60)
        client = FakeClient()
        payload = {
            "event_id": "e1", "decision": "recognized",
            "person": "Alice", "scenario_id": "s1",
        }
        self.assertTrue(dispatcher.dispatch(payload, client=client, prefix="faceid"))
        self.assertFalse(dispatcher.dispatch(payload, client=client, prefix="faceid"))
        self.assertEqual(client.messages[0][0], "faceid/v1/events")
        self.assertEqual(client.messages[0][1]["schema_version"], 1)

    def test_context_search_rows_decode_json(self):
        self.audit.start_event("e1", "front", time.time())
        self.audit.finalize("e1", "recognized", person="Alice")
        self.audit.update_context(
            "e1", description="red backpack", tags=["bag"], embedding=[1.0, 0.0]
        )
        row = self.audit.context_events()[0]
        self.assertEqual(row["ai_tags"], ["bag"])
        self.assertEqual(row["_embedding"], [1.0, 0.0])
