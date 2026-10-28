"""Ordering and safety tests for immediate confirmed recognition notifications."""
import json
import queue
import threading
from unittest.mock import patch
from paho.mqtt.client import MQTTErrorCode
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
import numpy as np

from app.mqtt_listener import EventProcessor
from app.integrations import IntegrationDispatcher
from app.frigate_api import FrigateAPI


class RecognitionLatencyTests(unittest.TestCase):
    def processor(self):
        gallery = Mock()
        gallery.persons.return_value = {}
        gallery.match_candidates.return_value = [('alice', 'Alice', .85), ('bob', 'Bob', .1)]
        gallery.match_ignored_detail.return_value = (0, None, None)
        processor = EventProcessor({'faceid': {}}, SimpleNamespace(), gallery, Mock())
        processor.client = Mock()
        processor.dispatcher = IntegrationDispatcher()
        processor._publish_presence = Mock()
        state = processor._new_event_state('live', 'door')
        processor.events['live'] = state
        return processor, state

    def face(self, processor, state, n):
        state['attempts'] += 1
        face = SimpleNamespace(bbox=np.array([0, 0, 80, 80]), det_score=.99,
                               normed_embedding=np.array([1., n / 10], dtype=np.float32))
        processor._process_face('live', state, np.zeros((100, 100, 3), dtype=np.uint8), face)

    def messages(self, processor, topic):
        return [json.loads(call.args[1]) for call in processor.client.publish.call_args_list
                if call.args[0] == topic]

    def test_confirmation_prepares_image_then_publishes_before_index_and_alert_storage(self):
        p, state = self.processor()
        order = []
        p.client.publish.side_effect = lambda topic, *a, **kw: order.append(topic)
        p.face_search = SimpleNamespace(record=lambda *a: order.append('face_index'))
        p.audit = SimpleNamespace(observation=lambda *a, **kw: order.append('observation'),
            save_evidence=lambda *a: order.append('image'), finalize=Mock())
        p.alerts = SimpleNamespace(record=lambda **kw: order.append('alert_store'))
        self.face(p, state, 1)
        self.assertNotIn('face_index', order)
        self.assertEqual(self.messages(p, 'faceid/v1/recognitions'), [])
        order.clear()
        self.face(p, state, 2)
        instant = self.messages(p, 'faceid/v1/recognitions')
        self.assertEqual(len(instant), 1)
        self.assertFalse(state['ended'])
        self.assertEqual(instant[0]['decision'], 'recognized')
        self.assertEqual(instant[0]['confirmations'], 2)
        self.assertLess(order.index('image'), order.index('faceid/v1/recognitions'))
        for work in ('face_index', 'alert_store', 'observation'):
            self.assertLess(order.index('faceid/v1/recognitions'), order.index(work))
        self.assertEqual(order.count('face_index'), 2)
        self.assertTrue(state['done'])

    def test_immediate_delivery_does_not_wait_for_event_end_or_duplicate_at_finalization(self):
        p, state = self.processor()
        self.face(p, state, 1); self.face(p, state, 2)
        self.assertEqual(len(self.messages(p, 'faceid/v1/recognitions')), 1)
        p._post_event('live', state, 'recognized', 'Alice', .85, confirmations=2)
        self.assertEqual(len(self.messages(p, 'faceid/v1/recognitions')), 1)
        self.assertEqual(len(self.messages(p, 'faceid/v1/events')), 1)
        self.assertIn('confirmed_to_notification', p.performance())

    def test_terminal_nonidentity_event_keeps_ha_event_without_inventing_person(self):
        p, state = self.processor()
        p._post_event('live', state, 'liveness_unconfirmed', None, 0)
        event = self.messages(p, 'faceid/v1/recognitions')[0]
        self.assertEqual(event['decision'], 'liveness_unconfirmed')
        self.assertIsNone(event['person'])
        self.assertFalse(any(x['decision'] == 'recognized' for x in self.messages(p, 'faceid/v1/recognitions')))

    def test_weak_margin_disagreement_and_duplicate_never_publish_confirmed(self):
        for mode in ('margin', 'disagreement', 'duplicate'):
            p, state = self.processor()
            for n in range(3):
                if mode == 'margin': p.gallery.match_candidates.return_value = [('alice', 'Alice', .85), ('bob', 'Bob', .82)]
                if mode == 'disagreement': p.gallery.match_candidates.return_value = [(str(n), str(n), .85)]
                self.face(p, state, 1 if mode == 'duplicate' else n)
            self.assertEqual(self.messages(p, 'faceid/v1/recognitions'), [])

    def test_fast_sampling_is_available_without_access1_and_stays_bounded(self):
        p, state = self.processor()
        p.camera_profiles = SimpleNamespace(get=lambda _: {'enabled': True, 'fast_recognition': True}, all=lambda: {})
        state['live_updated_at'] = time.time()
        p._schedule_live_jobs()
        self.assertEqual(p.queue.qsize(), 1)
        p._schedule_live_jobs()
        self.assertEqual(p.queue.qsize(), 1)
        self.assertEqual(p.policy.min_confirmations, 2)
        self.assertEqual(p.policy.match_threshold, .5)

    def test_fast_notification_reads_new_camera_frames_when_event_snapshot_is_unchanged(self):
        import cv2
        p, state = self.processor()
        profile = {'enabled': True, 'fast_recognition': True, 'high_resolution': True,
                   'min_face_px': 24, 'night_min_face_px': 24}
        p.camera_profiles = SimpleNamespace(get=lambda _: profile, all=lambda: {'door': profile})
        state['live_updated_at'] = time.time()
        p.frigate.snapshot.return_value = np.zeros((100, 100, 3), dtype=np.uint8)
        p.frigate.latest_frame_bytes.side_effect = [cv2.imencode('.jpg', np.full((100, 100, 3), n, dtype=np.uint8))[1].tobytes()
                                                  for n in (40, 100)]
        p.engine = SimpleNamespace(faces=Mock(return_value=[]))
        p._process('live'); p._process('live')
        self.assertEqual(p.engine.faces.call_count, 2)
        p.frigate.snapshot.assert_not_called()
        self.assertIsNone(state['access1_evidence_ts'])

    def evidence_face(self, p, state, n, quality_score, detection=.99):
        state['attempts'] += 1
        face = SimpleNamespace(bbox=np.array([0, 0, 80, 80]), det_score=detection,
                               normed_embedding=np.array([1., n / 10], dtype=np.float32))
        quality = SimpleNamespace(score=quality_score, sharpness=quality_score,
                                  to_dict=lambda: {'score': quality_score, 'sharpness': quality_score})
        p._process_face('live', state, np.full((100, 100, 3), n, dtype=np.uint8), face, quality=quality)

    def test_unknown_review_prefers_face_quality_over_detection_confidence(self):
        p, state = self.processor()
        p.gallery.match_candidates.return_value = [('alice', 'Alice', .2)]
        self.evidence_face(p, state, 1, .5, .99)
        self.evidence_face(p, state, 2, .9, .9)
        self.assertTrue(np.all(state['best_unknown']['crop'] == 2))

    def test_recognized_evidence_keeps_best_quality_matching_face(self):
        p, state = self.processor()
        p.audit = SimpleNamespace(observation=Mock(), save_evidence=Mock(), finalize=Mock())
        self.evidence_face(p, state, 1, .9)
        self.evidence_face(p, state, 2, .5)
        self.assertTrue(state['done'])
        self.assertTrue(np.all(p.audit.save_evidence.call_args.args[1] == 1))

    def test_recognized_image_is_ready_before_notification_and_alert_emission(self):
        p, state = self.processor()
        p.audit = SimpleNamespace(observation=Mock(), save_evidence=Mock(), finalize=Mock())
        self.evidence_face(p, state, 1, .9)
        def notification(*args, **kwargs):
            p.audit.save_evidence.assert_called_once()
            self.assertTrue(np.all(p.audit.save_evidence.call_args.args[1] == 1))
        p._publish_notification = notification
        self.evidence_face(p, state, 2, .5)

    def test_presence_update_also_keeps_best_matching_evidence(self):
        p, state = self.processor()
        p.audit = SimpleNamespace(observation=Mock(), save_evidence=Mock(), finalize=Mock())
        p.recognition_sessions = SimpleNamespace(classify=lambda *args: 'presence_update')
        self.evidence_face(p, state, 1, .9)
        self.evidence_face(p, state, 2, .5)
        p.audit.save_evidence.assert_called_once()
        self.assertTrue(np.all(p.audit.save_evidence.call_args.args[1] == 1))

    def test_recognized_evidence_never_uses_sharper_different_identity(self):
        p, state = self.processor()
        p.audit = SimpleNamespace(observation=Mock(), save_evidence=Mock(), finalize=Mock())
        p.gallery.match_candidates.return_value = [('bob', 'Bob', .85)]
        self.evidence_face(p, state, 1, .99)
        p.gallery.match_candidates.return_value = [('alice', 'Alice', .85)]
        self.evidence_face(p, state, 2, .8)
        self.evidence_face(p, state, 3, .5)
        self.assertTrue(np.all(p.audit.save_evidence.call_args.args[1] == 2))

    def test_fast_current_frames_cannot_supply_access_proof(self):
        import cv2
        p, state = self.processor()
        profile = {'enabled': True, 'fast_recognition': True, 'high_resolution': False,
                   'min_face_px': 24, 'night_min_face_px': 24}
        p.camera_profiles = SimpleNamespace(get=lambda _: profile, all=lambda: {'door': profile})
        p.access1 = SimpleNamespace(transport=object(), enqueue=Mock())
        p.frigate.latest_frame_bytes.side_effect = [cv2.imencode('.jpg', np.full((100, 100, 3), n, dtype=np.uint8))[1].tobytes()
                                                  for n in (40, 100)]
        faces = [SimpleNamespace(bbox=np.array([0, 0, 80, 80]), det_score=.99,
                                 normed_embedding=np.array([1., n / 10], dtype=np.float32)) for n in (1, 2)]
        p.engine = SimpleNamespace(faces=Mock(side_effect=[[face] for face in faces]))
        quality = SimpleNamespace(usable=True, score=.8, to_dict=lambda: {'score': .8})
        with patch('app.mqtt_listener.measure_face_quality', return_value=quality):
            p._process('live'); p._process('live')
        self.assertTrue(state['done'])
        self.assertEqual(p.access1.enqueue.call_args.args[0]['evidence_source'], 'live_camera')
        self.assertIsNone(p.access1.enqueue.call_args.args[0]['evidence_ts'])
        self.assertEqual(len(self.messages(p, 'faceid/v1/recognitions')), 1)

    def test_fast_full_camera_multiple_faces_cannot_assign_event_identity(self):
        import cv2
        p, state = self.processor()
        profile = {'enabled': True, 'fast_recognition': True, 'high_resolution': False,
                   'min_face_px': 24, 'night_min_face_px': 24}
        p.camera_profiles = SimpleNamespace(get=lambda _: profile, all=lambda: {'door': profile})
        p.audit = SimpleNamespace(observation=Mock(), save_evidence=Mock(), finalize=Mock())
        p.frigate.latest_frame_bytes.side_effect = [cv2.imencode('.jpg', np.full((100, 100, 3), n, dtype=np.uint8))[1].tobytes()
                                                  for n in (40, 100)]
        faces = [SimpleNamespace(bbox=np.array([0, 0, 40, 80]), det_score=.99,
                                 normed_embedding=np.array([1., n / 10], dtype=np.float32)) for n in (1, 2)]
        p.engine = SimpleNamespace(faces=Mock(return_value=faces))
        quality = SimpleNamespace(usable=True, score=.8, to_dict=lambda: {'score': .8})
        with patch('app.mqtt_listener.measure_face_quality', return_value=quality):
            p._process('live'); p._process('live')
        p.gallery.match_candidates.assert_not_called()
        self.assertEqual(state['decision'].observations, [])
        self.assertFalse(state['done'])
        self.assertEqual(self.messages(p, 'faceid/v1/recognitions'), [])
        self.assertEqual(p.audit.observation.call_count, 2)
        self.assertTrue(all(call.args[2] == 'multiple_faces' for call in p.audit.observation.call_args_list))

    def test_fast_notification_fallback_avoids_slow_recordings_and_respects_budget(self):
        p, state = self.processor()
        profile = {'enabled': True, 'fast_recognition': True, 'high_resolution': True,
                   'min_face_px': 24, 'night_min_face_px': 24}
        p.camera_profiles = SimpleNamespace(get=lambda _: profile, all=lambda: {'door': profile})
        p.frigate.latest_frame_bytes.return_value = None
        p.frigate.snapshot.return_value = np.zeros((100, 100, 3), dtype=np.uint8)
        p.engine = SimpleNamespace(faces=Mock(return_value=[]))
        p._process('live')
        p.frigate.snapshot.assert_called_once_with('live', crop=False)
        p.frigate.recording_frame.assert_not_called()
        state['live_fetches'] = 12
        p._process('live')
        p.frigate.latest_frame_bytes.assert_called_once_with('door', timeout=2.0)

    def test_live_frame_timeout_override_preserves_existing_preview_default(self):
        api = FrigateAPI('https://fixture.invalid', timeout=6, verify_tls=True)
        api.request = Mock(return_value=SimpleNamespace(status_code=200, content=b'image'))
        self.assertEqual(api.latest_frame_bytes('door'), b'image')
        self.assertEqual(api.request.call_args.kwargs['timeout'], 12)
        self.assertEqual(api.latest_frame_bytes('door', timeout=2.0), b'image')
        self.assertEqual(api.request.call_args.kwargs['timeout'], 2.0)
        self.assertTrue(api.session.verify)

    def test_ha_uses_immediate_topic_and_camera_specific_filters(self):
        p, state = self.processor()
        p.gallery.persons.return_value = {}
        p._publish_discovery(['door'])
        configs = [(c.args[0], json.loads(c.args[1])) for c in p.client.publish.call_args_list
                   if c.args[0].endswith('/config')]
        event = next(value for key, value in configs if '/event/' in key)
        self.assertEqual(event['state_topic'], 'faceid/v1/recognitions')
        trigger = next(value for key, value in configs if 'device_automation' in key and '_recognized/' in key)
        self.assertEqual(trigger['topic'], 'faceid/v1/recognitions')
        self.assertIn('value_json.camera', trigger['value_template'])
        self.assertIn('door', trigger['value_template'])


    def test_publication_is_visible_while_image_indexing_is_still_blocked(self):
        p, state = self.processor()
        entered, release = threading.Event(), threading.Event()
        def index(*args):
            entered.set(); release.wait(2)
        p.face_search = SimpleNamespace(record=index)
        self.face(p, state, 1)
        worker = threading.Thread(target=lambda: self.face(p, state, 2))
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            self.assertTrue(worker.is_alive())
            self.assertEqual(len(self.messages(p, 'faceid/v1/recognitions')), 1)
        finally:
            release.set(); worker.join(2)
        self.assertFalse(worker.is_alive())

    def test_access1_enqueue_precedes_nonessential_persistence(self):
        p, state = self.processor()
        order = []
        p.access1 = SimpleNamespace(enqueue=lambda _: order.append('access1'))
        p.face_search = SimpleNamespace(record=lambda *a: order.append('index'))
        self.face(p, state, 1); self.face(p, state, 2)
        self.assertLess(order.index('access1'), order.index('index'))

    def test_real_paho_definite_rejection_retries_same_payload_once_when_live(self):
        p, state = self.processor()
        accepted = False
        def publish(topic, *a, **kw):
            return SimpleNamespace(rc=MQTTErrorCode.MQTT_ERR_SUCCESS if accepted else MQTTErrorCode.MQTT_ERR_NO_CONN)
        p.client.publish.side_effect = publish
        self.face(p, state, 1); self.face(p, state, 2)
        self.assertFalse(state['notification_complete'])
        before = self.messages(p, 'faceid/v1/recognitions')[-1]
        accepted = True
        state['notification_next_retry'] = 0
        p._schedule_live_jobs()
        self.assertTrue(state['notification_complete'])
        self.assertEqual(self.messages(p, 'faceid/v1/recognitions')[-1], before)
        count = len(self.messages(p, 'faceid/v1/recognitions'))
        p._schedule_live_jobs()
        self.assertEqual(len(self.messages(p, 'faceid/v1/recognitions')), count)

    def test_expired_or_ended_failed_notification_never_replays_at_finalization(self):
        for ended in (False, True):
            p, state = self.processor()
            p.client.publish.return_value = SimpleNamespace(rc=MQTTErrorCode.MQTT_ERR_NO_CONN)
            self.face(p, state, 1); self.face(p, state, 2)
            count = len(self.messages(p, 'faceid/v1/recognitions'))
            state['notification_next_retry'] = 0
            if ended: state['ended'] = True
            else: state['notification_started'] -= 3
            p.client.publish.return_value = SimpleNamespace(rc=MQTTErrorCode.MQTT_ERR_SUCCESS)
            p._schedule_live_jobs()
            p._post_event('live', state, 'recognized', 'Alice', .85, confirmations=2)
            self.assertEqual(len(self.messages(p, 'faceid/v1/recognitions')), count)

    def test_webhook_rejection_retries_without_republishing_accepted_mqtt(self):
        p, state = self.processor()
        p.dispatcher.webhook_urls = ['https://fixture.invalid/notify']
        p.dispatcher._queue = queue.Queue(maxsize=1)
        p.dispatcher._queue.put_nowait({'busy': True})
        self.face(p, state, 1); self.face(p, state, 2)
        self.assertFalse(state['notification_webhooks'])
        self.assertFalse(state['notification_complete'])
        p.dispatcher._queue.get_nowait(); p.dispatcher._queue.task_done()
        state['notification_next_retry'] = 0
        p._schedule_live_jobs()
        self.assertTrue(state['notification_complete'])
        self.assertEqual(len(self.messages(p, 'faceid/v1/recognitions')), 1)
        self.assertEqual(p.dispatcher._queue.qsize(), 1)
        p._post_event('live', state, 'recognized', 'Alice', .85)
        self.assertEqual(p.dispatcher._queue.qsize(), 1)

    def test_unknown_acceptance_is_not_retried_as_a_new_automation(self):
        p, state = self.processor()
        p.client.publish.side_effect = RuntimeError('uncertain local write')
        self.face(p, state, 1); self.face(p, state, 2)
        count = len(self.messages(p, 'faceid/v1/recognitions'))
        state['notification_next_retry'] = 0
        p._schedule_live_jobs()
        self.assertEqual(len(self.messages(p, 'faceid/v1/recognitions')), count)

    def test_unknown_face_samples_survive_deferred_indexing(self):
        p, state = self.processor()
        p.gallery.match_candidates.return_value = [('alice', 'Alice', .2)]
        p.face_search = SimpleNamespace(record=Mock())
        self.face(p, state, 1); self.face(p, state, 2)
        p.face_search.record.assert_not_called()
        p._post_event('live', state, 'unknown', None, .2)
        self.assertEqual(p.face_search.record.call_count, 2)
        self.assertNotIn('pending_face_samples', state)
