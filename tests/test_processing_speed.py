import threading
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np

from app.mqtt_listener import EventProcessor
from app.audit import AuditStore


class ProcessingSpeedTests(unittest.TestCase):
    def processor(self):
        return EventProcessor({'faceid': {}}, SimpleNamespace(faces=lambda image: []), None,
                              SimpleNamespace(snapshot=lambda *args, **kwargs: np.zeros((80, 80, 3), dtype=np.uint8)))

    def test_clip_jobs_do_not_fill_the_live_queue_and_duplicates_coalesce(self):
        processor = self.processor()
        processor._enqueue_job('old', 'clip')
        processor._enqueue_job('new', 'snapshot')
        processor._enqueue_job('new', 'snapshot')
        self.assertEqual(processor.queue.qsize(), 1)
        self.assertEqual(processor.clip_queue.qsize(), 1)

    def test_live_job_completes_while_another_event_clip_is_blocked(self):
        processor = self.processor()
        entered, release, live = threading.Event(), threading.Event(), threading.Event()
        processor.events['old'] = processor._new_event_state('old', 'front')
        processor.events['new'] = processor._new_event_state('new', 'front')
        def clip(_):
            entered.set()
            release.wait(3)
        processor._process_clip = clip
        processor._process = lambda _: live.set()
        processor._enqueue_job('old', 'clip')
        processor._enqueue_job('new')
        thread = threading.Thread(target=lambda: processor._work_once(processor.clip_queue))
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            processor._work_once(processor.queue)
            self.assertTrue(live.is_set())
        finally:
            release.set()
            thread.join(3)

    def test_same_event_is_not_processed_concurrently(self):
        processor = self.processor()
        state = processor._new_event_state('same', 'front')
        processor.events['same'] = state
        state['processing_lock'].acquire()
        processor._process = lambda _: self.fail('Concurrent event processing')
        processor._enqueue_job('same')
        try:
            processor._work_once(processor.queue)
        finally:
            state['processing_lock'].release()
        processor._process = lambda _: state.update(processed=True)
        processor._flush_deferred_jobs()
        processor._work_once(processor.queue)
        self.assertTrue(state.get('processed'))

    def test_identical_snapshots_are_not_counted_as_independent_evidence(self):
        processor = self.processor()
        state = processor._new_event_state('same', 'front')
        processor.events['same'] = state
        processor._process('same')
        processor._process('same')
        self.assertEqual(state['attempts'], 1)

    def test_fast_profile_shortens_wait_without_changing_decision_policy(self):
        processor = self.processor()
        processor.camera_profiles = SimpleNamespace(get=lambda _: {'fast_recognition': True})
        self.assertLess(processor.recognition_interval('front'), 1)
        self.assertEqual(processor.policy.min_confirmations, 2)
        self.assertEqual(processor.policy.match_threshold, .5)
        processor.camera_profiles = None
        self.assertEqual(processor.recognition_interval('front'), 2.5)

    def test_metrics_are_bounded_and_do_not_report_fake_capture_latency(self):
        processor = self.processor()
        for _ in range(200):
            processor.record_timing('snapshot', .01)
        report = processor.performance()
        self.assertLessEqual(report['snapshot']['samples'], 128)
        self.assertAlmostEqual(report['snapshot']['p95_ms'], 10)
        self.assertNotIn('capture_to_recognition_ms', report)

    def test_finalizer_cannot_remove_or_publish_an_event_during_processing(self):
        processor = self.processor()
        processor.clip_analysis = False
        state = processor._new_event_state('same', 'front', end_time=1)
        processor.events['same'] = state
        published = []
        processor._post_event = lambda *args, **kwargs: published.append(args)
        state['processing_lock'].acquire()
        try:
            processor._finalize_events()
            self.assertIn('same', processor.events)
            self.assertEqual(published, [])
        finally:
            state['processing_lock'].release()
        processor._finalize_events()
        self.assertNotIn('same', processor.events)
        self.assertEqual(len(published), 1)

    def test_transient_inference_failure_retries_the_identical_image(self):
        processor = self.processor()
        calls = []
        def infer(image):
            calls.append(image)
            if len(calls) == 1:
                raise RuntimeError('temporary inference failure')
            return []
        processor.engine.faces = infer
        processor.events['retry'] = processor._new_event_state('retry', 'front')
        with self.assertRaises(RuntimeError):
            processor._process('retry')
        processor._process('retry')
        processor._process('retry')
        self.assertEqual(len(calls), 2)

    def test_persisted_clip_backlog_does_not_hide_live_snapshot_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            processor = self.processor()
            processor.audit = AuditStore(Path(tmp) / 'audit.db')
            for i in range(101):
                processor.audit.start_event(f'old-{i}', 'front', 1)
                processor.audit.queue_job(f'old-{i}', 'clip')
            processor.events['new'] = processor._new_event_state('new', 'front')
            self.assertTrue(processor._enqueue_job('new'))
            self.assertEqual(processor.queue.get_nowait()['eid'], 'new')
            self.assertLessEqual(processor.clip_queue.qsize(), 50)

    def test_alerts_receive_confirmed_encounters_even_when_presence_updates_are_quiet(self):
        processor = self.processor()
        processor.alerts = SimpleNamespace(record=Mock())
        processor._publish_presence = lambda *args, **kwargs: None
        state = processor._new_event_state('event', 'front')
        state['recognition_occurrence'] = 'presence_update'
        processor._publish_recognition('event', state, 'Alice', .8, decision_status='recognized')
        processor.alerts.record.assert_called_once_with(event_id='event', camera='front', status='recognized', person='Alice', score=.8, ts=state['start_time'])
        self.assertEqual(len(processor.recent), 0)


if __name__ == '__main__':
    unittest.main()
