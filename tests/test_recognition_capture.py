import asyncio
import json
from pathlib import Path
import queue
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app import recognition_capture as module
from app.acquisition_diagnostics import register_acquisition_routes
from app.camera_profiles import profile_capture_fields


def processor_fixture():
    profile = {'camera':'front', 'observation_source':'frigate_restream',
               'trust_local_video_network':True, 'enabled':True, 'recognition_mode':'event',
               'liveness_mode':'required', 'min_face_px':48, 'direct_rtsp_url':'rtsp://user:DO_NOT_EXPORT@10.0.0.1/live'}
    profiles = SimpleNamespace(get=lambda _: dict(profile), all=lambda: {'front':dict(profile)},
                               read_status=lambda: {'status':'ok', 'fallback_used':False})
    engine = SimpleNamespace(health=lambda: {'backend':'cpu', 'providers':['CPUExecutionProvider'],
        'cpu_tuning':{'threads':4, 'allow_spinning':False, 'applied':True},
        'runtime':{'insightface':'0.7.3'}, 'password':'DO_NOT_EXPORT',
        'stream_detection':{'frame_requests':11, 'unavailable_frames':2, 'rejected_frames':3,
                            'compute':{'p50_ms':415.2}, 'url':'rtsp://DO_NOT_EXPORT'},
        'embedding':{'calls':9, 'compute':{'p50_ms':1200}, 'person':'DO_NOT_EXPORT'}})
    processor = SimpleNamespace(camera_profiles=profiles, engine=engine, cameras={'front'},
        mqtt_health=lambda: {'connected':True}, queue=queue.Queue(), clip_queue=queue.Queue(),
        stream_observations=SimpleNamespace(lock=threading.RLock(), sessions={}, pending=set(),
                                            anchor_pending={}, report=lambda: {'active':[], 'recent':[]}),
        continuous=SimpleNamespace(lock=threading.RLock(), pending=set()),
        frigate=SimpleNamespace(request=Mock(side_effect=AssertionError('no camera requests'))))
    processor.recognition_capture = module.RecognitionCapture(processor)
    return processor, profile


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.processor, self.profile = processor_fixture()
        self.capture = self.processor.recognition_capture
        self.clock = [100.0]
        self.wall = [2000.0]
        self.cpu = [50.0]
        for target, effect in [('time.monotonic', lambda:self.clock[0]),
                               ('time.time', lambda:self.wall[0]),
                               ('time.process_time', lambda:self.cpu[0])]:
            patcher = patch('app.recognition_capture.'+target, side_effect=effect)
            patcher.start(); self.addCleanup(patcher.stop)
        patcher = patch.object(self.capture, '_run')
        patcher.start(); self.addCleanup(patcher.stop)

    def start(self, source='frigate_restream', seconds=300):
        return self.capture.start('admin', 'front', source, seconds)

    def test_passive_start_reads_safe_profile_and_never_changes_sources(self):
        before = dict(self.profile)
        result = self.start('snapshots')
        self.assertTrue(result['recording'])
        self.assertFalse(result['preflight']['source_matches'])
        self.assertEqual(result['before']['profile_revision'], profile_capture_fields(self.profile)['profile_revision'])
        self.assertEqual(result['mismatches'][0]['reason'], 'selected_saved_source')
        self.assertEqual(self.profile, before)
        self.processor.frigate.request.assert_not_called()
        exported = json.dumps(self.capture.report('admin'))
        self.assertNotIn('DO_NOT_EXPORT', exported)
        self.assertNotIn('direct_rtsp_url', exported)
        self.assertEqual(result['before']['engine']['stream_detection']['frame_requests'], 11)
        self.assertFalse(result['capture_starts_recognition'])
        self.assertTrue(result['normal_recognition_continues'])
        self.assertFalse(result['access1_disabled_by_build'])
        self.assertTrue(result['normal_recognition_and_access1_continue'])

    def test_fixed_deadline_and_old_event_flag_not_extended_by_hooks(self):
        self.start(seconds=10)
        module.emit(self.processor, 'e1', 'front', 'event_dispatch', dispatch_source='snapshots',
                    event_start_time=1999., event_type='update', retained=False)
        self.clock[0] = 109.5
        module.emit(self.processor, 'e1', 'front', 'event_dispatch', dispatch_source='snapshots')
        self.clock[0] = 111
        self.capture._tick()
        report = self.capture.report('admin')
        self.assertEqual(report['state'], 'complete')
        self.assertEqual(report['finish_reason'], 'deadline')
        self.assertTrue(report['events'][0]['started_before_capture'])
        self.assertEqual(report['mismatches'][0]['reason'], 'selected_dispatch_source')
        self.assertFalse(module.active(self.processor))

    def test_owner_confidentiality_single_flight_and_memory_expiration(self):
        self.start()
        with self.assertRaises(HTTPException) as error:
            self.capture.start('other', 'front', 'snapshots')
        self.assertEqual(error.exception.status_code, 409)
        for function in (self.capture.status, self.capture.report, self.capture.stop):
            with self.assertRaises(HTTPException) as error:
                function('other')
            self.assertEqual(error.exception.status_code, 403)
        self.capture.stop('admin')
        self.clock[0] += module.RESULT_TTL + 1
        self.capture._tick()
        self.assertEqual(self.capture.report('other')['state'], 'idle')
        self.assertIsNone(self.capture.owner)

    def test_scalar_allowlist_rejects_nested_credentials_names_vectors_and_bad_stages(self):
        self.start()
        module.emit(self.processor, 'e1', 'front', 'identity', score=.8, margin=.5,
                    name='DO_NOT_EXPORT', slug='DO_NOT_EXPORT', embedding=[1,2,3],
                    candidates=[{'password':'DO_NOT_EXPORT'}], reason='rtsp://user:DO_NOT_EXPORT@host',
                    error={'password':'DO_NOT_EXPORT'}, quality=float('nan'), elapsed_ms=55,
                    profile_revision='a'*16)
        module.emit(self.processor, 'e1', 'front', 'user:DO_NOT_EXPORT')
        module.emit(self.processor, 'e1', 'rtsp://DO_NOT_EXPORT@host', 'identity')
        report = self.capture.report('admin')
        record = report['events'][0]['records'][0]
        self.assertEqual(record['score'], .8)
        self.assertEqual(record['stage_elapsed_ms'], 55)
        self.assertEqual(record['profile_revision'], 'a'*16)
        self.assertNotIn('DO_NOT_EXPORT', json.dumps(report))
        self.assertNotIn('embedding', record)
        self.assertGreater(report['truncation']['fields'], 0)
        self.assertGreater(report['truncation']['records'], 0)

    def test_event_record_caps_keep_first_context_and_last_result(self):
        self.start()
        for index in range(module.MAX_EVENTS+4):
            module.emit(self.processor, f'e{index}', 'front', 'event_dispatch', event_type='new')
        for index in range(module.MAX_RECORDS+5):
            module.emit(self.processor, 'e0', 'front', 'frame', sequence=index)
        module.emit(self.processor, 'e0', 'front', 'access1_result', state='relay_activated')
        report = self.capture.report('admin')
        self.assertEqual(len(report['events']), module.MAX_EVENTS)
        self.assertEqual(len(report['events'][0]['records']), module.MAX_RECORDS)
        self.assertEqual(report['events'][0]['records'][0]['stage'], 'event_dispatch')
        self.assertEqual(report['events'][0]['records'][-1]['stage'], 'access1_result')
        self.assertEqual(report['truncation']['events'], 4)
        self.assertGreater(report['truncation']['records'], 0)
        status = self.capture.status('admin')
        self.assertLessEqual(len(status['events']), 8)
        self.assertLessEqual(len(status['events'][0]['records']), 9)

    def test_repeated_identical_wait_is_coalesced_but_later_wait_retained(self):
        self.start()
        for _ in range(4):
            module.emit(self.processor, 'e1', 'front', 'fresh_frame_wait', reason='unchanged_timestamp')
            self.clock[0] += .2
        self.clock[0] += 1
        module.emit(self.processor, 'e1', 'front', 'fresh_frame_wait', reason='unchanged_timestamp')
        report = self.capture.report('admin')
        self.assertEqual(len(report['events'][0]['records']), 2)
        self.assertEqual(report['truncation']['coalesced_records'], 3)

    def test_process_resources_are_aggregate_and_include_quota_and_throttle_delta(self):
        throttle = {'cgroup_version':2, 'periods':10, 'throttled_periods':2, 'throttled_usec':1000}
        with patch.object(module, '_cpu_throttle', side_effect=lambda:dict(throttle)), patch.object(module, '_rss_bytes', return_value=123456), patch.object(module, '_runtime_limits', return_value={'cpu_quota_cpus':2}):
            self.start()
            self.clock[0] += 1
            self.cpu[0] += 1.5
            throttle.update(periods=20, throttled_periods=5, throttled_usec=4000)
            self.capture._tick()
        report = self.capture.report('admin')
        sample = report['resources'][0]
        self.assertEqual(sample['process_cpu_percent_one_core'], 150)
        self.assertEqual(sample['process_cpu_percent_quota'], 75)
        self.assertEqual(sample['rss_bytes'], 123456)
        self.assertEqual(sample['cgroup_cpu_throttle']['throttled_usec_delta'], 3000)
        self.assertIn('whole_process', report['resource_measurement'])
        self.assertIn('not_per_event', sample['cgroup_cpu_throttle']['scope'])

    def test_preview_pair_owner_ttl_caps_and_images_never_export(self):
        self.start()
        image = np.full((80,80,3), 200, np.uint8)
        module.emit(self.processor, 'e1', 'front', 'event_dispatch')
        module.preview(self.processor, 'e1', 'front', image, 'anchor')
        module.preview(self.processor, 'e1', 'front', image, 'input')
        module.preview(self.processor, 'e1', 'front', image, 'subject', bbox=[10,10,50,50])
        self.assertEqual(len(self.capture.previews), 3)
        original = self.capture.get_preview('admin', 'e1', 'anchor')
        module.preview(self.processor, 'e1', 'front', np.zeros_like(image), 'anchor')
        self.assertEqual(self.capture.get_preview('admin', 'e1', 'anchor'), original)
        with self.assertRaises(HTTPException):
            self.capture.get_preview('other', 'e1', 'input')
        for i in range(2,7):
            module.emit(self.processor, f'e{i}', 'front', 'event_dispatch')
            module.preview(self.processor, f'e{i}', 'front', image, 'anchor')
        self.assertEqual(len({key[0] for key in self.capture.previews}), 4)
        report = self.capture.report('admin')
        self.assertNotIn('base64', json.dumps(report))
        self.assertNotIn('image', report)
        self.clock[0] += module.PREVIEW_TTL+1
        with self.assertRaises(HTTPException) as error:
            self.capture.get_preview('admin', 'e1', 'anchor')
        self.assertEqual(error.exception.status_code, 404)

    def test_inactive_and_broken_recorder_hooks_do_not_throw_or_read_profiles(self):
        self.processor.camera_profiles.get = Mock(side_effect=AssertionError('no inactive reads'))
        module.emit(self.processor, 'e1', 'front', 'event_dispatch', score=.8)
        module.preview(self.processor, 'e1', 'front', object(), 'anchor')
        self.processor.camera_profiles.get.assert_not_called()
        self.processor.recognition_capture = SimpleNamespace(is_active=Mock(side_effect=RuntimeError('secret')),
            emit=Mock(side_effect=RuntimeError('secret')), preview=Mock(side_effect=RuntimeError('secret')))
        self.assertFalse(module.active(self.processor))
        module.emit(self.processor, 'e1', 'front', 'event_dispatch')
        module.preview(self.processor, 'e1', 'front', object(), 'input')

    def test_first_anchor_recovers_after_invalid_image_and_encoder_failure(self):
        self.start()
        module.emit(self.processor, 'e1', 'front', 'event_dispatch')
        image = np.full((80,80,3), 200, np.uint8)
        module.preview(self.processor, 'e1', 'front', object(), 'anchor')
        self.assertEqual(self.capture.preview_times, {})
        with patch('cv2.imencode', side_effect=RuntimeError('DO_NOT_EXPORT')):
            module.preview(self.processor, 'e1', 'front', image, 'anchor')
        self.assertEqual(self.capture.anchor_saved, set())
        self.assertEqual(self.capture.preview_pending, set())
        self.clock[0] += 1.1
        module.preview(self.processor, 'e1', 'front', image, 'anchor')
        self.assertTrue(self.capture.get_preview('admin', 'e1', 'anchor'))
        self.assertNotIn('DO_NOT_EXPORT', json.dumps(self.capture.report('admin')))

    def test_actual_snapshot_hook_metadata_survives_real_export(self):
        self.start()
        stamp = profile_capture_fields(self.profile)
        module.emit(self.processor, 'e1', 'front', 'event_dispatch', **stamp,
                    event_type='new', label='person', dispatch_source='frigate_restream',
                    event_start_time=2000., profile_read_status='ok', profile_fallback_used=False)
        module.emit(self.processor, 'e1', 'front', 'association_candidates', candidate_count=2,
                    usable_count=1, roi_rejected_count=0, quality_rejected_count=1)
        module.emit(self.processor, 'e1', 'front', 'association_match', association_score=.8,
                    alternative_score=.2, association_margin=.6, reverse_margin=.4)
        record = self.capture.report('admin')['events'][0]['records']
        self.assertEqual(record[0]['profile_revision'], self.capture.report('admin')['before']['profile_revision'])
        self.assertEqual(record[1]['quality_rejected_count'], 1)
        self.assertEqual(record[2]['association_margin'], .6)


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.processor, self.profile = processor_fixture()
        self.capture = self.processor.recognition_capture
        patcher = patch.object(self.capture, '_run')
        patcher.start(); self.addCleanup(patcher.stop)
        app = FastAPI()
        register_acquisition_routes(app, self.processor, Path('/not-used'))
        self.client = TestClient(app)
        self.owner = 'admin'
        patcher = patch('app.acquisition_diagnostics.require_pairing_admin', new=AsyncMock(side_effect=lambda request:self.owner))
        patcher.start(); self.addCleanup(patcher.stop)

    def test_start_status_fresh_full_report_stop_and_no_store(self):
        response = self.client.post('/api/recognition-capture', json={'action':'start', 'camera':'front', 'expected_source':'frigate_restream'})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['recording'])
        module.emit(self.processor, 'e1', 'front', 'event_dispatch', event_type='new')
        current = self.client.get('/api/acquisition-diagnostics').json()['recognition_capture']
        self.assertEqual(current['events'][0]['event_id'], 'e1')
        module.emit(self.processor, 'e2', 'front', 'event_dispatch', event_type='new')
        response = self.client.get('/api/recognition-capture')
        self.assertEqual(len(response.json()['events']), 2)
        self.assertIn('no-store', response.headers['cache-control'])
        self.assertEqual(self.client.post('/api/recognition-capture', json={'action':'stop'}).json()['state'], 'stopped')
        self.assertEqual(self.client.get('/api/recognition-capture/report').status_code, 200)
        self.processor.frigate.request.assert_not_called()

    def test_invalid_actions_duration_camera_and_body_are_rejected_without_echo(self):
        for body in [{'action':'start','camera':'front','expected_source':'snapshots','duration_seconds':601},
                     {'action':'start','camera':'front','expected_source':'snapshots','duration_seconds':True},
                     {'action':'start','camera':'rtsp://SECRET@host','expected_source':'snapshots'},
                     {'action':'start','camera':'front','expected_source':[]},
                     {'action':'stop','password':'SECRET'}, {'action':'open'}, []]:
            response = self.client.post('/api/recognition-capture', json=body)
            self.assertEqual(response.status_code, 400, response.text)
            self.assertNotIn('SECRET', response.text)
        response = self.client.post('/api/recognition-capture', content=b'x'*4097)
        self.assertEqual(response.status_code, 400)

    def test_other_admin_cannot_read_report_but_existing_diagnostic_page_still_works(self):
        self.capture.start('admin', 'front', 'frigate_restream')
        self.owner = 'other'
        self.assertEqual(self.client.get('/api/recognition-capture').status_code, 403)
        response = self.client.get('/api/acquisition-diagnostics')
        self.assertEqual(response.status_code, 200)
        status = response.json()['recognition_capture']
        self.assertEqual(status['state'], 'busy')
        self.assertNotIn('camera', status)

    def test_preview_requires_owner_and_private_no_store_headers(self):
        self.capture.start('admin', 'front', 'frigate_restream')
        module.emit(self.processor, 'e1', 'front', 'event_dispatch')
        module.preview(self.processor, 'e1', 'front', np.zeros((80,80,3),np.uint8), 'anchor')
        response = self.client.get('/api/recognition-capture/preview/e1/anchor')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['cache-control'], 'private, no-store')
        self.assertEqual(response.headers['cross-origin-resource-policy'], 'same-origin')
        self.owner = 'other'
        self.assertEqual(self.client.get('/api/recognition-capture/preview/e1/anchor').status_code, 403)

    def test_routes_use_real_guard_without_authorization_bypass(self):
        with patch('app.acquisition_diagnostics.require_pairing_admin', new=AsyncMock(side_effect=HTTPException(403,'Denied'))):
            self.assertEqual(self.client.get('/api/recognition-capture').status_code, 403)
            self.assertEqual(self.client.post('/api/recognition-capture',json={'action':'stop'}).status_code, 403)
            self.assertEqual(self.client.get('/api/recognition-capture/preview/e/anchor').status_code, 403)

    def test_recorder_api_failures_are_generic_and_do_not_break_existing_status(self):
        self.processor.recognition_capture = SimpleNamespace(
            stop=Mock(side_effect=RuntimeError('rtsp://SECRET@host')),
            report=Mock(side_effect=RuntimeError('rtsp://SECRET@host')),
            status=Mock(side_effect=RuntimeError('rtsp://SECRET@host')))
        for response in (self.client.get('/api/recognition-capture'),
                         self.client.post('/api/recognition-capture',json={'action':'stop'})):
            self.assertEqual(response.status_code, 503)
            self.assertNotIn('SECRET', response.text)
        response = self.client.get('/api/acquisition-diagnostics')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['recognition_capture']['state'], 'unavailable')


class AutonomousDeadlineTests(unittest.TestCase):
    def test_capture_ends_without_status_poll_or_open_page(self):
        processor, _ = processor_fixture()
        capture = processor.recognition_capture
        capture.start('admin', 'front', 'frigate_restream', 1)
        deadline = time.monotonic()+3
        while time.monotonic() < deadline:
            with capture.lock:
                if not capture.run['recording']:
                    break
            time.sleep(.05)
        with capture.lock:
            self.assertFalse(capture.run['recording'])
            self.assertEqual(capture.run['finish_reason'], 'deadline')
            capture.run = None
        capture.wake.set()


if __name__ == '__main__':
    unittest.main()
