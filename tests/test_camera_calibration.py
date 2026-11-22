import base64
from contextlib import nullcontext
from types import SimpleNamespace
import threading
import time
import unittest
from unittest.mock import Mock, patch

import cv2
import numpy as np

from app.camera_calibration import CameraCalibration, CalibrationUnavailable
from app.engine import InferenceLock
from app.live_frames import LiveFramePool, LiveFrameReader


class CameraCalibrationTests(unittest.TestCase):
    def setUp(self):
        self.image = np.random.default_rng(7).integers(0, 255, (120, 160, 3), dtype=np.uint8)
        self.content = cv2.imencode('.jpg', self.image)[1].tobytes()
        self.profile = {'observation_source':'direct_rtsp','min_face_px':48}
        self.revision = 'one'
        self.reader = SimpleNamespace(generation=4, stopped=threading.Event(), frame_record=Mock())
        self.record = {'image':self.image,'sequence':2,'generation':4,'arrived':time.monotonic()}
        self.reader.frame_record.return_value = self.record
        self.profiles = SimpleNamespace(get=lambda c:dict(self.profile),
            observation_config=lambda c:(dict(self.profile),'rtsp://user:secret@10.1.2.3:554/primary',self.revision))
        self.frigate = SimpleNamespace(_live_url=Mock(return_value='rtsp://frigate:8554/door'),
            latest_frame_bytes=Mock(return_value=self.content),main_stream_frame_bytes=Mock(return_value=self.content),
            live_frames=SimpleNamespace(acquire_calibration=Mock(return_value=self.reader)))
        self.processor = SimpleNamespace(camera_profiles=self.profiles,frigate=self.frigate,
            min_face_px=48,min_face_quality=.35,access1=Mock())
        self.engine = SimpleNamespace(background_work=Mock(side_effect=lambda **k:nullcontext()),
            detect_frame_record=Mock(side_effect=lambda r:(r.frame_record(),[])),detect_faces=Mock(return_value=[]),faces=Mock())
        self.calibration = CameraCalibration(self.processor,self.engine)

    def test_direct_uses_saved_source_no_frigate_fallback_or_access(self):
        result = self.calibration.analyze('door',True)
        self.assertEqual(result['source'],'direct_rtsp')
        self.assertEqual((result['width'],result['height']),(160,120))
        self.frigate.live_frames.acquire_calibration.assert_called_once_with('door','rtsp://user:secret@10.1.2.3:554/primary')
        self.frigate.latest_frame_bytes.assert_not_called();self.frigate._live_url.assert_not_called()
        self.engine.faces.assert_not_called();self.processor.access1.assert_not_called()
        self.assertFalse(result['access_evidence'])
        self.assertNotIn('secret',str(result));self.assertNotIn('10.1.2.3',str(result))
        decoded=cv2.imdecode(np.frombuffer(base64.b64decode(result['preview'].split(',')[1]),np.uint8),cv2.IMREAD_COLOR)
        self.assertEqual(decoded.shape,self.image.shape)

    def test_restream_uses_same_pool_and_latest_record_after_admission(self):
        self.profile['observation_source']='frigate_restream'
        result=self.calibration.analyze('door')
        self.frigate.live_frames.acquire_calibration.assert_called_with('door','rtsp://frigate:8554/door')
        self.engine.detect_frame_record.assert_called_once_with(self.reader)
        self.assertNotIn('preview',result)

    def test_snapshots_preserve_existing_frame_choice_and_exact_jpeg(self):
        self.profile['observation_source']='snapshots'
        result=self.calibration.analyze('door',True)
        self.assertEqual(base64.b64decode(result['preview'].split(',')[1]),self.content)
        self.engine.detect_faces.assert_called_once();self.engine.detect_frame_record.assert_not_called()
        self.frigate.live_frames.acquire_calibration.assert_not_called()
        self.assertEqual(result['frame_kind'],'snapshot_preview')

    def test_snapshot_high_resolution_continuous_choice_unchanged(self):
        self.profile.update(observation_source='snapshots',recognition_mode='continuous',high_resolution=True)
        self.calibration.frame('door')
        self.frigate.main_stream_frame_bytes.assert_called_once_with('door')
        self.frigate.latest_frame_bytes.assert_not_called()

    def test_starting_or_invalid_stream_never_falls_back(self):
        self.reader.frame_record.return_value=None
        for action in (self.calibration.frame,self.calibration.analyze):
            with self.assertRaisesRegex(CalibrationUnavailable,'no_valid_frame'):action('door')
        self.frigate.latest_frame_bytes.assert_not_called()

    def test_capacity_rejected_without_detection(self):
        self.frigate.live_frames.acquire_calibration.return_value=None
        with self.assertRaisesRegex(CalibrationUnavailable,'capacity'):self.calibration.analyze('door')
        self.engine.detect_frame_record.assert_not_called()

    def test_generation_change_during_detection_rejects_result(self):
        def detect(reader):reader.generation+=1;return self.record,[]
        self.engine.detect_frame_record.side_effect=detect
        with self.assertRaisesRegex(CalibrationUnavailable,'source_changed'):self.calibration.analyze('door')

    def test_configuration_change_during_detection_rejects_result(self):
        def detect(reader):self.revision='two';return self.record,[]
        self.engine.detect_frame_record.side_effect=detect
        with self.assertRaisesRegex(CalibrationUnavailable,'source_changed'):self.calibration.analyze('door')

    def test_untrusted_network_is_diagnostic_only_and_no_access(self):
        self.profile['trust_local_video_network']=False
        self.assertFalse(self.calibration.analyze('door')['access_evidence'])
        self.assertEqual(self.processor.access1.mock_calls,[])

    def test_reader_exception_does_not_expose_authenticated_url(self):
        self.frigate.live_frames.acquire_calibration.side_effect=RuntimeError('rtsp://user:secret@10.1.2.3/primary')
        with self.assertRaises(CalibrationUnavailable) as error:self.calibration.frame('door')
        self.assertEqual(str(error.exception),'configured_source_unavailable')
        self.assertFalse(self.calibration.gate.locked())

    def test_busy_requests_never_queue(self):
        self.calibration.gate.acquire()
        try:
            with self.assertRaisesRegex(CalibrationUnavailable,'calibration_busy'):self.calibration.analyze('door')
            self.engine.detect_frame_record.assert_not_called()
        finally:self.calibration.gate.release()

    def test_scheduler_busy_is_categorical_and_releases_request_gate(self):
        self.engine.detect_frame_record.side_effect=TimeoutError('busy')
        with self.assertRaisesRegex(CalibrationUnavailable,'recognition_busy'):self.calibration.analyze('door')
        self.assertFalse(self.calibration.gate.locked())

    def test_public_direct_address_rejected(self):
        self.profiles.observation_config=lambda c:(dict(self.profile),'rtsp://8.8.8.8/live','one')
        with self.assertRaises(CalibrationUnavailable):self.calibration.frame('door')
        self.frigate.live_frames.acquire_calibration.assert_not_called()


class CalibrationLeaseTests(unittest.TestCase):
    def setUp(self):
        self.start=patch.object(LiveFrameReader,'start').start()
        self.timer=patch('app.live_frames.threading.Timer').start()
        self.pool=LiveFramePool()
    def tearDown(self):
        self.pool.close();patch.stopall()
    def test_borrows_operational_reader_without_releasing_observation(self):
        reader=self.pool.acquire_observation('door','rtsp://fixture/main')
        self.assertIs(self.pool.acquire_calibration('door','rtsp://fixture/main'),reader)
        self.pool.calibration_sources['door']=0
        self.pool._expire_calibration()
        self.assertFalse(reader.stopped.is_set())
        self.assertIn('door',self.pool.observation_sources)
        self.assertEqual(self.start.call_count,1)
    def test_observation_finishes_calibration_lease_then_expires(self):
        reader=self.pool.acquire_observation('door','rtsp://fixture/main')
        self.pool.acquire_calibration('door','rtsp://fixture/main')
        self.pool.release_observation('door')
        self.pool.sync({})
        self.assertFalse(reader.stopped.is_set())
        self.pool.calibration_sources['door']=0;self.pool._expire_calibration()
        self.assertTrue(reader.stopped.is_set());self.assertFalse(self.pool.readers)
    def test_never_replaces_production_source(self):
        reader=self.pool.acquire_observation('door','rtsp://fixture/main')
        self.assertIsNone(self.pool.acquire_calibration('door','rtsp://fixture/other'))
        self.assertFalse(reader.stopped.is_set())
    def test_one_calibration_camera_and_no_jpeg_loop(self):
        reader=self.pool.acquire_calibration('door','rtsp://fixture/main')
        self.assertFalse(reader.encode_jpeg)
        self.assertIsNone(self.pool.acquire_calibration('yard','rtsp://fixture/yard'))
    def test_retiring_decoder_is_counted(self):
        reader=self.pool.acquire_calibration('door','rtsp://fixture/main')
        reader.thread=Mock(is_alive=Mock(return_value=True))
        self.pool.calibration_sources['door']=0;self.pool._expire_calibration()
        self.pool.max_readers=1
        self.assertIsNone(self.pool.acquire_calibration('yard','rtsp://fixture/yard'))


class CalibrationSchedulerTests(unittest.TestCase):
    def test_diagnostic_wait_is_bounded_and_does_not_steal_live_reservation(self):
        lock=InferenceLock()
        with lock.live_work():
            with lock.background_work(timeout=.01):
                with self.assertRaises(TimeoutError):
                    with lock:pass
        with lock:pass
        self.assertFalse(lock.live_activity()['active'])
    def test_diagnostic_does_not_mark_foreground_activity(self):
        lock=InferenceLock();before=lock.live_activity()['last_activity_monotonic']
        with lock.background_work(timeout=.1):
            with lock:pass
        self.assertEqual(before,lock.live_activity()['last_activity_monotonic'])
