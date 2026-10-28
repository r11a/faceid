import unittest
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import requests
import cv2
import numpy as np
from fastapi.testclient import TestClient

from app.frigate_api import FrigateAPI, camera_frame_bytes
from app.webui import build_app


class CameraFrameSourceTests(unittest.TestCase):
    def test_preview_and_analysis_use_the_same_full_resolution_source_as_sampling(self):
        with tempfile.TemporaryDirectory() as folder:
            image = np.zeros((1080, 1920, 3), dtype=np.uint8)
            jpeg = cv2.imencode('.jpg', image)[1].tobytes()
            frigate = Mock()
            frigate.cameras.return_value = ['office']
            frigate.main_stream_frame_bytes.return_value = jpeg
            profile = {'recognition_mode': 'presence', 'high_resolution': True, 'min_face_px': 48}
            processor = SimpleNamespace(frigate=frigate, cameras={'office'}, audit=None,
                camera_profiles=SimpleNamespace(get=lambda camera: profile, all=lambda: {'office': profile}), min_face_px=48, min_face_quality=.35)
            gallery = SimpleNamespace()
            app = build_app({'faceid': {'auth': {}}}, SimpleNamespace(faces=lambda image: []),
                            gallery, processor, Path(folder), Path(__file__).parents[1] / 'static')
            client = TestClient(app)
            preview = client.get('/api/cameras/office/frame')
            self.assertEqual(preview.status_code, 200)
            self.assertEqual(preview.content, jpeg)
            analyzed = client.get('/api/cameras/office/analyze')
            self.assertEqual(analyzed.status_code, 200)
            self.assertEqual((analyzed.json()['width'], analyzed.json()['height']), (1920, 1080))
            frigate.latest_frame_bytes.assert_not_called()
            self.assertEqual(frigate.main_stream_frame_bytes.call_count, 2)
            frigate.main_stream_frame_bytes.return_value = None
            self.assertEqual(client.get('/api/cameras/office/analyze').status_code, 404)

    def test_preview_joins_an_inflight_camera_capture_instead_of_starting_another(self):
        api = self.api()
        entered, release = threading.Event(), threading.Event()
        original = api.request.side_effect
        config_response = next(original)
        def request(method, path, **kwargs):
            if path == '/api/config':
                return config_response
            entered.set()
            release.wait(3)
            return self.response()
        api.request.side_effect = request
        results = []
        worker = threading.Thread(target=lambda: results.append(api.main_stream_frame_bytes('office')))
        worker.start()
        self.assertTrue(entered.wait(2))
        # Observe entry into the follower's wait deterministically, not via a sleep.
        flight = api._main_pending['office']
        real_wait = flight['ready'].wait
        def joined(timeout):
            release.set()
            return real_wait(timeout)
        flight['ready'].wait = joined
        results.append(api.main_stream_frame_bytes('office'))
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results, [b'jpeg', b'jpeg'])
        self.assertEqual(api.request.call_count, 2)

    def response(self, data=None, content=b'jpeg', status=200):
        return Mock(status_code=status, content=content, json=Mock(return_value=data))

    def api(self, live=None, camera='office', streams=None):
        api = FrigateAPI('https://frigate.invalid', verify_tls=True)
        config = {'cameras': {camera: {'live': {'streams': live or {}}}},
                  'go2rtc': {'streams': streams or {'office': []}}}
        api.request = Mock(side_effect=[self.response(config), self.response()])
        return api

    def test_event_mode_keeps_original_source_even_when_high_resolution_is_enabled(self):
        api = Mock()
        camera_frame_bytes(api, 'office', {'recognition_mode': 'event', 'high_resolution': True}, timeout=2)
        api.latest_frame_bytes.assert_called_once_with('office', timeout=2)
        api.main_stream_frame_bytes.assert_not_called()

    def test_native_modes_use_main_source_only_when_enabled(self):
        for mode in ('presence', 'continuous'):
            api = Mock()
            camera_frame_bytes(api, 'office', {'recognition_mode': mode, 'high_resolution': True}, timeout=2)
            api.main_stream_frame_bytes.assert_called_once_with('office')
            api.latest_frame_bytes.assert_not_called()

    def test_configured_live_stream_alias_is_used_with_bounded_authenticated_request(self):
        api = self.api({'Main': 'doorbell_main'}, camera='doorbell', streams={'doorbell_main': []})
        self.assertEqual(api.main_stream_frame_bytes('doorbell'), b'jpeg')
        call = api.request.call_args
        self.assertEqual(call.args, ('GET', '/api/ffprobe/snapshot'))
        self.assertEqual(call.kwargs['params'], {'url': 'rtsp://127.0.0.1:8554/doorbell_main', 'timeout': 5})
        self.assertEqual(call.kwargs['timeout'], 6)
        self.assertTrue(api.session.verify)

    def test_camera_named_stream_and_cached_config(self):
        api = self.api()
        api.request.side_effect = [self.response({'cameras': {'office': {}}, 'go2rtc': {'streams': {'office': []}}}), self.response(), self.response()]
        self.assertEqual(api.main_stream_frame_bytes('office'), b'jpeg')
        self.assertEqual(api.main_stream_frame_bytes('office'), b'jpeg')
        self.assertEqual(sum(c.args[1] == '/api/config' for c in api.request.call_args_list), 1)

    def test_unknown_source_never_guesses_a_camera_url(self):
        api = self.api(streams={'other': []})
        self.assertIsNone(api.main_stream_frame_bytes('office'))
        self.assertEqual(api.request.call_count, 1)

    def test_failure_never_falls_back_to_low_resolution_or_archived_image(self):
        for outcome in (self.response(status=403), self.response(content=b''), requests.Timeout()):
            api = self.api()
            api.request.side_effect = [self.response({'cameras': {'office': {}}, 'go2rtc': {'streams': {'office': []}}}), outcome]
            self.assertIsNone(api.main_stream_frame_bytes('office'))
            self.assertFalse(any('latest' in c.args[1] or 'events/' in c.args[1] for c in api.request.call_args_list))

    def test_stream_name_cannot_inject_rtsp_authority_or_query(self):
        for name in ('../other', 'x?src=other', 'x#other', 'user@elsewhere'):
            api = self.api({'Main': name}, streams={name: []})
            self.assertIsNone(api.main_stream_frame_bytes('office'))
            self.assertEqual(api.request.call_count, 1)
