"""Offline passive-capture parity: no real camera, relay or external services."""
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import numpy as np
from fastapi.testclient import TestClient

from app.camera_profiles import CameraProfiles, profile_capture_fields
from app.recognition_capture import RecognitionCapture
from app.timing import stage, frame_wait
from app.webui import build_app
import test_snapshot_subject_association as association
from test_webui_clip import FakeGallery


class NormalCaptureHooksTests(unittest.TestCase):
    def fixture(self, capture='inactive'):
        helper = association.SnapshotAttributionTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        p = helper.p
        if capture == 'active':
            with patch('app.recognition_capture.threading.Thread.start'):
                p.recognition_capture.start('admin', 'front', 'snapshots', 60)
        elif capture == 'failed':
            p.recognition_capture = SimpleNamespace(
                is_active=Mock(side_effect=RuntimeError('capture unavailable')),
                emit=Mock(side_effect=RuntimeError('capture unavailable')),
                preview=Mock(side_effect=RuntimeError('capture unavailable')))
        return helper

    def records(self, p, eid='e'):
        report = p.recognition_capture.report('admin')
        return next(event['records'] for event in report['events'] if event['event_id'] == eid)

    def test_inactive_active_and_failed_capture_keep_same_snapshot_models_and_subject(self):
        for mode in ('inactive', 'active', 'failed'):
            with self.subTest(mode=mode):
                helper = self.fixture(mode)
                subject = association.face()
                helper.run_frame(faces=[subject, association.face((0, 1, 0), (200, 0, 280, 80))])
                self.assertIs(helper.p._process_face.call_args.args[3], subject)
                self.assertEqual(helper.p.engine.detect_faces.call_count, 2)
                self.assertEqual(helper.p.engine.embed_face.call_count, 3)
                helper.p.frigate.association_frame.assert_called_once()
                if mode == 'active':
                    rows = self.records(helper.p)
                    stages = {row['stage'] for row in rows}
                    self.assertTrue({'snapshot_acquisition', 'anchor_fetch', 'anchor_result',
                                     'subject_assignment', 'snapshot_candidate_embeddings'} <= stages)
                    stamp = next(row['profile_revision'] for row in rows if row['stage'] == 'snapshot_eligibility')
                    self.assertEqual(stamp, helper.p.recognition_capture.report('admin')['before']['profile_revision'])
                helper.doCleanups()

    def test_assignment_details_explain_single_person_anchor_mismatch(self):
        helper = self.fixture('active')
        helper.run_frame(faces=[association.face((0, 1, 0))])
        helper.p._process_face.assert_not_called()
        rows = self.records(helper.p)
        decision = next(row for row in rows if row['stage'] == 'subject_assignment')
        self.assertEqual(decision['candidate_count'], 1)
        self.assertEqual(decision['anchor_count'], 1)
        self.assertEqual(decision['reason'], 'anchor_similarity_below_threshold')
        self.assertAlmostEqual(decision['association_score'], 0)
        self.assertFalse(decision['accepted'])

    def test_timing_and_freshness_are_captured_when_global_timing_is_off(self):
        helper = self.fixture('active')
        p = helper.p
        self.assertFalse(p.timing_diagnostics)
        with patch('app.timing.log.info') as logging:
            with stage(p, 'e', 'existing_model_stage'):
                pass
            frame_wait(p, 'e', helper.st, 'unchanged_capture_timestamp')
        logging.assert_not_called()
        self.assertNotIn('timing_wait_logged', helper.st)
        rows = self.records(p)
        self.assertTrue(any(row['stage'] == 'existing_model_stage' and 'duration_ms' in row for row in rows))
        self.assertTrue(any(row['stage'] == 'fresh_frame_wait' for row in rows))

    def test_incoming_nonperson_and_disabled_camera_are_visible_before_early_return(self):
        helper = self.fixture('active')
        helper.p._ensure_discovery = Mock()
        for eid, label, enabled in (('car-event', 'car', True), ('disabled-event', 'person', False)):
            helper.profile['enabled'] = enabled
            after = {'id': eid, 'camera': 'front', 'label': label, 'start_time': time.time()-10,
                     'has_snapshot': True, 'false_positive': True}
            helper.p._on_message(None, None, SimpleNamespace(topic='frigate/events',
                payload=json.dumps({'type': 'update', 'after': after}).encode(), retain=True))
            rows = self.records(helper.p, eid)
            received = next(row for row in rows if row['stage'] == 'frigate_event_received')
            self.assertTrue(received['retained'])
            self.assertTrue(received['false_positive'])
            self.assertEqual(received['label'], label)
            self.assertTrue(any(row['stage'] == 'event_skipped' for row in rows))
        report = helper.p.recognition_capture.report('admin')
        self.assertTrue(all(event['started_before_capture'] for event in report['events']))
        self.assertTrue(helper.p.queue.empty())

    def test_expected_restream_but_snapshot_dispatch_is_explicit_without_rerouting(self):
        helper = self.fixture()
        p = helper.p
        with patch('app.recognition_capture.threading.Thread.start'):
            p.recognition_capture.start('admin', 'front', 'frigate_restream', 60)
        p._ensure_discovery = Mock()
        p._on_message(None, None, SimpleNamespace(topic='frigate/events', retain=False,
            payload=json.dumps({'type': 'new', 'after': {'id': 'new-event', 'camera': 'front',
                                'label': 'person', 'has_snapshot': True, 'start_time': time.time()}}).encode()))
        rows = self.records(p, 'new-event')
        self.assertTrue(any(row.get('dispatch_source') == 'snapshots' for row in rows))
        report = p.recognition_capture.report('admin')
        self.assertTrue(any(row['reason'] == 'selected_dispatch_source' for row in report['mismatches']))
        self.assertEqual(p.queue.qsize(), 1)
        self.assertNotIn('new-event', p.stream_observations.sessions)

    def test_pending_snapshot_skip_does_not_create_model_work(self):
        helper = self.fixture('active')
        helper.st['live_snapshot_consumed_ts'] = helper.st['live_snapshot_ts']
        helper.p._schedule_live_jobs()
        self.assertTrue(helper.p.queue.empty())
        helper.p.engine.detect_faces.assert_not_called()
        self.assertTrue(any(row.get('reason') == 'unchanged_capture_timestamp' for row in self.records(helper.p)))

    def test_pad_rejection_is_reported_without_changing_decision(self):
        helper = self.fixture('active')
        del helper.p._process_face
        helper.p.liveness = SimpleNamespace(
            analyze=Mock(return_value={'state': 'spoof', 'live': False, 'score': .2, 'threshold': .5, 'face_px': 80}),
            consensus=Mock(return_value={'state': 'spoof', 'confirmed': False, 'live_frames': 0, 'required_frames': 3}))
        helper.p.gallery = Mock()
        helper.run_frame()
        helper.p.gallery.match_candidates.assert_not_called()
        rows = self.records(helper.p)
        pad = next(row for row in rows if row['stage'] == 'liveness_result')
        self.assertEqual(pad['liveness_state'], 'spoof')
        self.assertAlmostEqual(pad['liveness_score'], .2)
        self.assertFalse(pad['accepted'])


class ProfileCaptureHooksTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'camera_profiles.json'
        self.profiles = CameraProfiles(self.path)

    def test_read_metadata_does_not_change_missing_invalid_or_valid_fallback(self):
        self.assertEqual(self.profiles.read_status(), {'status': 'not_read', 'fallback_used': False})
        self.assertEqual(self.profiles.get('front')['observation_source'], 'snapshots')
        self.assertEqual(self.profiles.read_status(), {'status': 'missing', 'fallback_used': True})
        for contents, status in (('{bad', 'invalid_json'), ('[]', 'invalid_shape'), ('{}', 'ok')):
            self.path.write_text(contents)
            self.assertEqual(self.profiles.get('front')['observation_source'], 'snapshots')
            self.assertEqual(self.profiles.read_status()['status'], status)
        with patch.object(Path, 'read_text', side_effect=PermissionError('secret path')):
            self.assertEqual(self.profiles.all(), {})
            self.assertEqual(self.profiles.read_status(), {'status': 'unreadable', 'fallback_used': True})

    def test_safe_stamp_does_not_depend_on_rtsp_credential(self):
        base = {'observation_source': 'direct_rtsp', 'trust_local_video_network': True,
                'direct_rtsp_configured': True, 'direct_rtsp_url': 'rtsp://user:secret@10.0.0.1/stream'}
        first = profile_capture_fields(base)
        second = profile_capture_fields({**base, 'direct_rtsp_url': 'rtsp://other:password@10.0.0.2/stream'})
        self.assertEqual(first, second)
        self.assertNotIn('secret', json.dumps(first))
        self.assertNotIn('direct_rtsp_url', first)

    def client(self, active=True):
        processor = SimpleNamespace(audit=None, media_store=None,
            frigate=SimpleNamespace(cameras=lambda: ['front'], timeout=1),
            camera_profiles=self.profiles, cameras=set(), min_face_px=48)
        processor.recognition_capture = RecognitionCapture(processor)
        if active:
            with patch('app.recognition_capture.threading.Thread.start'):
                processor.recognition_capture.start('admin', 'front', 'frigate_restream', 60)
        client = TestClient(build_app({'faceid': {'auth': {}}}, SimpleNamespace(), FakeGallery(),
                           processor, Path(self.temp.name), Path(__file__).parents[1] / 'static'))
        return client, processor

    def test_profile_success_records_requested_saved_and_readback_source(self):
        client, p = self.client()
        payload = {'min_face_px': 80, 'role': 'entry', 'observation_source': 'frigate_restream',
                   'trust_local_video_network': True}
        with patch('app.access1_admin.require_pairing_admin', new_callable=AsyncMock):
            response = client.post('/api/cameras/front/profile', json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.profiles.get('front')['observation_source'], 'frigate_restream')
        row = next(row for row in p.recognition_capture.report('admin')['records'] if row['stage'] == 'profile_save_result')
        self.assertEqual(row['requested_source'], 'frigate_restream')
        self.assertEqual(row['saved_source'], 'frigate_restream')
        self.assertEqual(row['persisted_source'], 'frigate_restream')
        self.assertTrue(row['source_matches_request'])
        self.assertTrue(row['trust_matches_request'])
        self.assertEqual(row['profile_revision'], profile_capture_fields(self.profiles.get('front'))['profile_revision'])
        p.recognition_capture.stop('admin')
        self.assertEqual(row['profile_revision'], p.recognition_capture.report('admin')['after']['profile_revision'])

    def test_readback_error_preserves_successful_save_and_response(self):
        client, p = self.client()
        original = self.profiles.get
        calls = []
        def read(camera):
            calls.append(camera)
            if len(calls) == 3:
                raise OSError('private diagnostic failure')
            return original(camera)
        with patch.object(self.profiles, 'get', side_effect=read):
            response = client.post('/api/cameras/front/profile', json={'min_face_px': 80, 'role': 'entry'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(original('front')['min_face_px'], 80)
        row = next(row for row in p.recognition_capture.report('admin')['records'] if row['stage'] == 'profile_save_result')
        self.assertEqual(row['reason'], 'profile_readback_unavailable')
        self.assertEqual(row['http_status'], 200)

    def test_invalid_profile_is_same_http_error_and_never_leaks_body(self):
        client, p = self.client()
        response = client.post('/api/cameras/front/profile', json={'min_face_px': 3, 'role': 'entry'})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.path.exists())
        report = p.recognition_capture.report('admin')
        self.assertTrue(any(row.get('reason') == 'profile_validation_failed' for row in report['records']))
        malformed = client.post('/api/cameras/front/profile', json={'min_face_px': 'rtsp://admin:secret@10.0.0.1'})
        self.assertEqual(malformed.status_code, 422)
        self.assertNotIn('secret', json.dumps(p.recognition_capture.report('admin')))

    def test_inactive_capture_does_not_add_profile_readback(self):
        client, _ = self.client(active=False)
        with patch.object(self.profiles, 'get', wraps=self.profiles.get) as read:
            response = client.post('/api/cameras/front/profile', json={'min_face_px': 80, 'role': 'entry'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(read.call_count, 2)  # existing pre-save read + update's return


if __name__ == '__main__':
    unittest.main()
