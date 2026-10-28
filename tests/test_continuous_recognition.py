import json
import tempfile
import time
import unittest
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import cv2
import numpy as np

from app.audit import AuditStore
from app.camera_profiles import CameraProfiles
from app.continuous_recognition import ContinuousRecognition
from app.integrations import IntegrationDispatcher
from app.mqtt_listener import EventProcessor


class DeferredExecutor:
    def __init__(self):
        self.jobs = []

    def submit(self, fn, *args):
        future = Future()
        self.jobs.append((fn, args, future))
        return future


class ContinuousRecognitionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.gallery = Mock()
        self.gallery.persons.return_value = {}
        self.gallery.match_ignored_detail.return_value = (0, None, None)
        self.gallery.match_candidates.side_effect = lambda emb, **kw: (
            [('alice', 'Alice', .9), ('bob', 'Bob', .1)] if emb[0] > emb[2]
            else [('bob', 'Bob', .9), ('alice', 'Alice', .1)])
        self.engine = SimpleNamespace(faces=Mock(return_value=[]))
        self.frigate = Mock()
        self.audit = AuditStore(root / 'audit.db')
        self.p = EventProcessor({'faceid': {}}, self.engine, self.gallery, self.frigate, self.audit)
        self.p.camera_profiles = CameraProfiles(root / 'cameras.json')
        self.p.client = Mock()
        self.p.dispatcher = IntegrationDispatcher()
        self.p.alerts = SimpleNamespace(record=Mock())
        self.p._publish_presence = Mock()
        self.service = self.p.continuous
        self.executor = DeferredExecutor()
        self.service.executor = self.executor
        self.set_mode('continuous')
        quality = SimpleNamespace(usable=True, score=.8, sharpness=.8,
                                  to_dict=lambda: {'score': .8, 'sharpness': .8})
        self.quality_patch = patch('app.continuous_recognition.measure_face_quality', return_value=quality)
        self.quality_patch.start()
        self.addCleanup(self.quality_patch.stop)
        self.counter = 0

    def set_mode(self, mode, camera='door', **extra):
        self.p.camera_profiles.update(camera, min_face_px=24, role='entry',
                                     recognition_mode=mode, liveness_mode='off', **extra)

    def face(self, who='alice', n=1, x=0):
        emb = np.array([1, n / 100, 0] if who == 'alice' else [0, n / 100, 1], dtype=np.float32)
        return SimpleNamespace(bbox=np.array([x, 0, x + 60, 80]), det_score=.99,
                               normed_embedding=emb / np.linalg.norm(emb))

    def frame(self, faces, now=None, camera='door', duplicate=False):
        if not duplicate:
            self.counter += 1
        self.frigate.latest_frame_bytes.return_value = cv2.imencode(
            '.jpg', np.full((120, 200, 3), self.counter, dtype=np.uint8))[1].tobytes()
        self.engine.faces.return_value = faces
        with patch('app.continuous_recognition.time.time', return_value=now or time.time()):
            self.service.sample_camera(camera, self.p.camera_profiles.get(camera))

    def messages(self):
        return [json.loads(c.args[1]) for c in self.p.client.publish.call_args_list
                if c.args[0] == 'faceid/v1/recognitions' and json.loads(c.args[1])['decision'] == 'recognized']

    def mqtt(self, topic, value, retained=False):
        self.p._on_message(self.p.client, None, SimpleNamespace(topic=topic,
            payload=(value if isinstance(value, str) else json.dumps(value)).encode(), retain=retained))

    def person(self, eid, kind='new', camera='door'):
        self.mqtt('frigate/events', {'type': kind, 'after': {'id': eid, 'camera': camera,
                  'label': 'person', 'start_time': time.time(), 'has_snapshot': True,
                  'end_time': time.time() if kind == 'end' else None}})

    def test_continuous_recognizes_without_person_event_and_saves_image_before_alert(self):
        self.frame([self.face(n=1)])
        self.assertEqual(self.messages(), [])
        self.frame([self.face(n=2)])
        result = self.messages()
        self.assertEqual([m['person'] for m in result], ['Alice'])
        self.assertTrue(result[0]['event_id'].startswith('faceid-'))
        self.assertTrue(self.audit.evidence_path(result[0]['event_id']).is_file())
        self.p.alerts.record.assert_called_once()
        self.assertEqual(self.p.queue.qsize(), 0)

    def test_two_people_get_independent_consensus_even_when_face_order_changes(self):
        self.frame([self.face('alice', 1), self.face('bob', 1, 100)])
        self.frame([self.face('bob', 2, 100), self.face('alice', 2)])
        self.assertEqual({m['person'] for m in self.messages()}, {'Alice', 'Bob'})
        self.assertEqual(len({m['event_id'] for m in self.messages()}), 2)

    def test_newcomer_recognizes_while_first_person_remains(self):
        self.frame([self.face(n=1)])
        self.frame([self.face(n=2)])
        self.frame([self.face(n=3), self.face('bob', 1, 100)])
        self.frame([self.face(n=4), self.face('bob', 2, 100)])
        self.assertEqual([m['person'] for m in self.messages()], ['Alice', 'Bob'])

    def test_same_person_reentry_not_suppressed_by_five_minute_session(self):
        now = time.time()
        self.frame([self.face(n=1)], now)
        self.frame([self.face(n=2)], now + .5)
        self.frame([], now + 3)
        self.frame([self.face(n=3)], now + 3.5)
        self.frame([self.face(n=4)], now + 4)
        self.assertEqual([m['person'] for m in self.messages()], ['Alice', 'Alice'])
        self.assertNotEqual(self.messages()[0]['event_id'], self.messages()[1]['event_id'])

    def test_duplicate_frame_never_supplies_second_confirmation_or_rearms_visible_track(self):
        now = time.time()
        self.frame([self.face(n=1)], now)
        for n in range(1, 10):
            self.frame([self.face(n=1)], now + n, duplicate=True)
        self.assertEqual(self.messages(), [])
        self.frame([self.face(n=2)], now + 9.5)
        self.assertEqual(len(self.messages()), 1)
        self.assertEqual(self.engine.faces.call_count, 2)

    def test_sampling_continues_after_legacy_attempt_and_time_limits(self):
        now = time.time()
        for n in range(20):
            self.frame([], now + n * 2)
        self.frame([self.face(n=1)], now + 41)
        self.frame([self.face(n=2)], now + 41.5)
        self.assertEqual(len(self.messages()), 1)

    def test_presence_requires_person_and_stops_when_last_event_ends(self):
        self.set_mode('presence')
        self.frame([self.face(n=1)])
        self.frigate.latest_frame_bytes.assert_not_called()
        self.person('first')
        self.person('second')
        self.person('first', 'end')
        self.frame([self.face(n=1)])
        self.frame([self.face(n=2)])
        self.assertEqual(len(self.messages()), 1)
        self.person('second', 'end')
        self.frigate.latest_frame_bytes.reset_mock()
        self.frame([self.face('bob')])
        self.frigate.latest_frame_bytes.assert_not_called()
        self.assertEqual(self.p.queue.qsize(), 0)

    def test_presence_accepts_person_counts_but_not_motion_or_zone_counts(self):
        self.set_mode('presence')
        self.mqtt('frigate/some_zone/person', 1, retained=True)
        self.mqtt('frigate/door/motion', 'ON')
        self.frame([self.face()])
        self.frigate.latest_frame_bytes.assert_not_called()
        self.mqtt('frigate/door/person', 1, retained=True)
        self.frame([self.face(n=1)])
        self.frame([self.face(n=2)])
        self.assertEqual(len(self.messages()), 1)
        self.mqtt('frigate/door/person', 0)
        self.frigate.latest_frame_bytes.reset_mock()
        self.frame([])
        self.frigate.latest_frame_bytes.assert_not_called()

    def test_disabled_during_fetch_discards_result(self):
        def disable(*args, **kwargs):
            self.p.camera_profiles.set_enabled('door', False)
            return cv2.imencode('.jpg', np.zeros((120, 200, 3), np.uint8))[1].tobytes()
        self.frigate.latest_frame_bytes.side_effect = disable
        self.service.sample_camera('door', self.p.camera_profiles.get('door'))
        self.engine.faces.assert_not_called()
        self.assertEqual(self.messages(), [])

    def test_changed_mode_during_inference_discards_result(self):
        self.frame([self.face(n=1)])
        self.engine.faces.side_effect = lambda img: (self.set_mode('event') or [self.face(n=2)])
        self.frame([self.face(n=2)])
        self.assertEqual(self.messages(), [])

    def test_continuous_frames_never_enqueue_access1_admission(self):
        self.p.access1 = SimpleNamespace(transport=object(), enqueue=Mock())
        self.frame([self.face(n=1)])
        self.frame([self.face(n=2)])
        self.assertEqual(len(self.messages()), 1)
        self.p.access1.enqueue.assert_not_called()

    def test_event_mode_keeps_legacy_queue_and_does_not_sample_continuously(self):
        self.set_mode('event')
        self.person('legacy')
        self.assertEqual(self.p.queue.qsize(), 1)
        self.frame([self.face()])
        self.frigate.latest_frame_bytes.assert_not_called()

    def test_scheduler_bounds_inflight_jobs_and_does_not_queue_old_frames(self):
        for camera in ('b', 'c', 'd'):
            self.set_mode('continuous', camera)
        for n in range(20):
            self.service.tick(time.time() + n)
        camera_jobs = [j for j in self.executor.jobs if j[0].__name__ == '_run_camera']
        self.assertEqual(len(camera_jobs), 2)
        self.assertEqual(len({j[1][0] for j in camera_jobs}), 2)

    def test_unknown_consensus_memory_is_bounded_during_long_presence(self):
        self.gallery.match_candidates.return_value = []
        self.gallery.match_candidates.side_effect = lambda *a, **k: []
        now = time.time()
        for n in range(90):
            self.frame([self.face(n=1 + n / 100)], now + n / 2)
        states = self.service.cameras['door']['states']
        self.assertEqual(len(states), 1)
        self.assertLessEqual(len(next(iter(states.values()))['decision'].observations), 32)
        self.assertEqual(self.messages(), [])

    def test_latest_frame_failure_never_falls_back_to_old_event_snapshot(self):
        self.frigate.latest_frame_bytes.return_value = None
        self.service.sample_camera('door', self.p.camera_profiles.get('door'))
        self.engine.faces.assert_not_called()
        self.frigate.snapshot.assert_not_called()

    def test_small_faces_report_why_no_identity_observation_was_created(self):
        quality = SimpleNamespace(usable=False, face_px=12, detection=.8, score=.6,
                                  to_dict=lambda: {'face_px': 12, 'detection': .8, 'score': .6})
        with patch('app.continuous_recognition.measure_face_quality', return_value=quality):
            self.frame([self.face()])
        status = self.service.report()['cameras']['door']
        self.assertEqual(status['state'], 'faces_rejected')
        self.assertEqual(status['largest_face_px'], 12)
        self.assertEqual(status['rejected_reasons'], {'small_face': 1})
        self.assertEqual(status['frame_width'], 200)
        self.assertEqual(status['frame_height'], 120)
        self.assertEqual(status['usable_faces'], 0)
        self.assertEqual(self.messages(), [])

    def test_full_resolution_profile_uses_main_frame_and_reports_source_failure(self):
        self.set_mode('continuous', high_resolution=True)
        self.frigate.main_stream_frame_bytes.return_value = None
        self.service.sample_camera('door', self.p.camera_profiles.get('door'))
        self.frigate.main_stream_frame_bytes.assert_called_once_with('door')
        self.frigate.latest_frame_bytes.assert_not_called()
        self.assertEqual(self.service.report()['cameras']['door']['source'], 'main_stream')
        self.assertEqual(self.service.report()['cameras']['door']['state'], 'no_frame')

    def test_reconcile_restores_presence_after_startup_and_removes_missed_end(self):
        self.set_mode('presence')
        response = Mock(status_code=200)
        response.json.return_value = [{'id': 'first', 'camera': 'door', 'label': 'person', 'end_time': None}]
        self.frigate.request.return_value = response
        self.service._reconcile_active()
        self.frame([self.face(n=1)])
        self.frame([self.face(n=2)])
        self.assertEqual(len(self.messages()), 1)
        response.json.return_value = []
        self.service._reconcile_active()
        self.frigate.latest_frame_bytes.reset_mock()
        self.frame([])
        self.frigate.latest_frame_bytes.assert_not_called()

    def test_reconcile_cannot_overwrite_newer_person_message(self):
        self.set_mode('presence')
        response = Mock(status_code=200)
        response.json.side_effect = lambda: (self.person('just-arrived') or [])
        self.frigate.request.return_value = response
        self.service._reconcile_active()
        self.frame([self.face(n=1)])
        self.frame([self.face(n=2)])
        self.assertEqual(len(self.messages()), 1)

    def test_offline_then_reconnect_retries_presence_recovery(self):
        self.set_mode('presence')
        self.mqtt('frigate/available', 'offline')
        self.frame([self.face()])
        self.frigate.latest_frame_bytes.assert_not_called()
        self.service.reconnect()
        self.service.tick()
        self.assertTrue(any(job[0].__name__ == '_reconcile_active' for job in self.executor.jobs))

    def test_failed_camera_releases_slot_and_next_camera_gets_turn(self):
        for camera in ('b', 'c'):
            self.set_mode('continuous', camera)
        self.service.tick()
        initially_started = {job[1][0] for job in self.executor.jobs}
        not_started = {'door', 'b', 'c'} - initially_started
        first, args, future = self.executor.jobs.pop(0)
        self.frigate.latest_frame_bytes.side_effect = RuntimeError('camera unavailable')
        with self.assertLogs('faceid.continuous', level='ERROR'):
            first(*args)
        self.assertNotIn(args[0], self.service.pending)
        self.service.tick(time.time() + 1)
        starts = [job[1][0] for job in self.executor.jobs]
        self.assertEqual(set(starts), (initially_started - {args[0]}) | not_started)
        self.assertEqual(len(self.service.pending), 2)

    def test_confirmed_sighting_finalizes_with_no_synthetic_frigate_event_request(self):
        now = time.time()
        self.p.set_sub_label = True
        self.frame([self.face(n=1)], now)
        self.frame([self.face(n=2)], now + .5)
        eid = self.messages()[0]['event_id']
        self.frame([], now + 3)
        self.p._finalize_events(now + 8)
        self.assertEqual(self.audit.event_detail(eid)['event']['status'], 'recognized')
        self.frigate.set_sub_label.assert_not_called()
        self.frigate.snapshot.assert_not_called()
        self.assertEqual(len(self.messages()), 1)

    def test_unknown_sighting_keeps_review_image_at_end_and_bounded_diagnostics(self):
        self.gallery.match_candidates.side_effect = lambda *a, **k: []
        now = time.time()
        for n in range(130):
            self.frame([self.face(n=1 + n / 1000)], now + n / 2)
        eid = next(iter(self.service.cameras['door']['states']))
        self.assertLessEqual(len(self.audit.event_detail(eid)['observations']), 96)
        self.frame([], now + 70)
        self.p._finalize_events(now + 75)
        self.gallery.save_unknown.assert_called_once()
        self.assertTrue(self.audit.evidence_path(eid).is_file())
        self.frigate.recording_frame.assert_not_called()

    def test_best_evidence_improves_after_confirmation_without_second_alert(self):
        self.frame([self.face(n=1)])
        self.frame([self.face(n=2)])
        eid = self.messages()[0]['event_id']
        before = self.audit.evidence_path(eid).read_bytes()
        self.quality_patch.stop()
        quality = SimpleNamespace(usable=True, score=.95, sharpness=.95,
                                  to_dict=lambda: {'score': .95, 'sharpness': .95})
        with patch('app.continuous_recognition.measure_face_quality', return_value=quality):
            self.frame([self.face(n=3)])
        self.assertNotEqual(self.audit.evidence_path(eid).read_bytes(), before)
        self.assertEqual(len(self.messages()), 1)

    def test_access_only_legacy_event_cannot_duplicate_continuous_alert_at_finalization(self):
        state = self.p._new_event_state('access-proof', 'door', access_only=True)
        self.p._post_event('access-proof', state, 'recognized', 'Alice', .9)
        published = [c.args[0] for c in self.p.client.publish.call_args_list]
        self.assertNotIn('faceid/v1/events', published)
        self.assertNotIn('faceid/v1/recognitions', published)

    def test_disable_during_image_save_cancels_publication_and_alert(self):
        self.frame([self.face(n=1)])
        original = self.audit.save_evidence
        def disable(eid, image):
            result = original(eid, image)
            self.p.camera_profiles.set_enabled('door', False)
            return result
        with patch.object(self.audit, 'save_evidence', side_effect=disable):
            self.frame([self.face(n=2)])
        self.assertEqual(self.messages(), [])
        self.p.alerts.record.assert_not_called()

    def test_done_face_refreshes_presence_without_repeating_alert(self):
        now = time.time()
        self.frame([self.face(n=1)], now)
        self.frame([self.face(n=2)], now + .5)
        self.frame([self.face(n=3)], now + 150)
        self.assertEqual(self.p.present['door']['Alice'], now + 150)
        self.assertEqual(len(self.messages()), 1)

    def test_unrelated_camera_update_does_not_prevent_presence_reconciliation(self):
        self.set_mode('presence')
        self.set_mode('presence', 'busy')
        self.person('missed-end')
        response = Mock(status_code=200)
        response.json.side_effect = lambda: (self.person('busy-event', camera='busy') or [])
        self.frigate.request.return_value = response
        self.service._reconcile_active()
        self.assertFalse(self.service.eligible('door', self.p.camera_profiles.get('door')))
        self.assertTrue(self.service.eligible('busy', self.p.camera_profiles.get('busy')))

    def test_disabled_camera_does_not_retry_pending_recognition(self):
        from paho.mqtt.client import MQTTErrorCode
        self.p.client.publish.return_value = SimpleNamespace(rc=MQTTErrorCode.MQTT_ERR_NO_CONN)
        self.frame([self.face(n=1)])
        self.frame([self.face(n=2)])
        run = self.service.cameras['door']
        state = next(iter(run['states'].values()))
        self.assertFalse(state['notification_complete'])
        self.p.camera_profiles.set_enabled('door', False)
        state['notification_next_retry'] = 0
        self.p.client.publish.reset_mock()
        self.service._retry_notifications(run)
        self.p.client.publish.assert_not_called()

    def test_policy_raise_applies_to_already_tracked_unconfirmed_faces(self):
        self.frame([self.face(n=1)])
        self.p.update_decision_policy(match_thr=.95)
        self.frame([self.face(n=2)])
        self.assertEqual(self.messages(), [])

    def test_policy_raise_discards_votes_that_only_met_old_threshold(self):
        self.frame([self.face(n=1)])
        self.p.update_decision_policy(match_thr=.95)
        self.gallery.match_candidates.side_effect = lambda *a, **kw: [('alice', 'Alice', .99)]
        self.frame([self.face(n=2)])
        self.assertEqual(self.messages(), [])
        self.frame([self.face(n=3)])
        self.assertEqual(len(self.messages()), 1)

    def test_native_recognition_persists_confirmation_time_for_delayed_video(self):
        now = time.time()
        self.gallery.match_candidates.side_effect = lambda *a, **kw: []
        self.frame([self.face(n=1)], now)
        self.gallery.match_candidates.side_effect = lambda *a, **kw: [('alice', 'Alice', .9)]
        self.frame([self.face(n=2)], now + 120)
        self.frame([self.face(n=3)], now + 120.5)
        eid = self.messages()[0]['event_id']
        self.assertEqual(self.audit.event_metadata(eid)['recognition_ts'], now + 120.5)

    def test_native_guest_recognition_does_not_authorize_admission(self):
        self.gallery.match_candidates.side_effect = lambda *a, **kw: []
        self.p.guest_access = SimpleNamespace(threshold=.8, margin=.08,
            candidates=Mock(return_value=[('guest:visitor', 'Visitor', .99)]), evaluate=Mock(return_value={'allowed': True}))
        self.frame([self.face(n=1)])
        self.frame([self.face(n=2)])
        self.assertEqual([m['person'] for m in self.messages()], ['Visitor'])
        self.p.guest_access.evaluate.assert_not_called()
        self.assertFalse(any(c.args[0] == 'faceid/v1/guest_access' for c in self.p.client.publish.call_args_list))

    def test_camera_wide_sightings_do_not_train_or_identify_a_bystanders_body(self):
        self.p.reid = SimpleNamespace(seed=Mock(), match=Mock())
        self.p.body_recognition = SimpleNamespace(predict=Mock(return_value={'advisory': True}), add_pending=Mock())
        now = time.time()
        self.frame([self.face(n=1)], now)
        self.frame([self.face(n=2)], now + .5)
        self.frame([], now + 3)
        self.p._finalize_events(now + 8)
        self.p.reid.seed.assert_not_called()
        self.p.body_recognition.predict.assert_not_called()
        self.p.body_recognition.add_pending.assert_not_called()


if __name__ == '__main__':
    unittest.main()
