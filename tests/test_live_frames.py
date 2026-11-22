import threading
import unittest
from unittest.mock import Mock, patch

import numpy as np

from app.live_frames import LiveFramePool, LiveFrameReader


class LiveFrameTests(unittest.TestCase):
    def test_decoder_backlog_is_not_reported_as_a_fresh_frame(self):
        reader = LiveFrameReader('rtsp://fixture/office')
        self.assertFalse(reader.accept_position(1000, 10, 10))  # Drain startup packets.
        self.assertTrue(reader.accept_position(2000, 11, 10))
        self.assertFalse(reader.accept_position(2040, 13, 10))  # A stalled decoder resumes old packets.
        self.assertTrue(reader.accept_position(4000, 13, 10))  # Caught up with the stream clock.
        self.assertFalse(reader.accept_position(1000, 13.1, 10))  # Timestamp reset requires reconnect.

    def test_only_latest_fresh_frame_is_returned(self):
        reader = LiveFrameReader('rtsp://fixture/office')
        with patch('app.live_frames.time.monotonic', return_value=10):
            reader.publish(b'first', 1920, 1080)
        with patch('app.live_frames.time.monotonic', return_value=10.25):
            reader.publish(b'latest', 1920, 1080)
            self.assertEqual(reader.frame(), b'latest')
        with patch('app.live_frames.time.monotonic', return_value=12):
            self.assertIsNone(reader.frame())
            self.assertEqual(reader.report()['state'], 'stale')

    def test_stop_invalidates_frame_immediately(self):
        reader = LiveFrameReader('rtsp://fixture/office')
        reader.publish(b'image', 1920, 1080)
        reader.stop()
        self.assertIsNone(reader.frame())

    def test_same_camera_reuses_reader_and_disabled_camera_is_stopped(self):
        with patch.object(LiveFrameReader, 'start') as start:
            pool = LiveFramePool(max_readers=2)
            reader = pool.ensure('office', 'rtsp://fixture/office')
            self.assertIs(pool.ensure('office', 'rtsp://fixture/office'), reader)
            self.assertEqual(start.call_count, 1)
            pool.sync({})
            self.assertTrue(reader.stopped.is_set())
            self.assertEqual(pool.report(), {})

    def test_capacity_counts_readers_still_stopping(self):
        with patch.object(LiveFrameReader, 'start'):
            pool = LiveFramePool(max_readers=1)
            first = pool.ensure('office', 'rtsp://fixture/office')
            first.thread = Mock(is_alive=Mock(return_value=True))
            pool.sync({})
            self.assertIsNone(pool.ensure('door', 'rtsp://fixture/door'))
            first.thread.is_alive.return_value = False
            self.assertIsNotNone(pool.ensure('door', 'rtsp://fixture/door'))
            pool.close()

    def test_changed_stream_stops_old_reader_without_reusing_its_frame(self):
        with patch.object(LiveFrameReader, 'start'):
            pool = LiveFramePool(max_readers=2)
            old = pool.ensure('office', 'rtsp://fixture/old')
            old.publish(b'old', 320, 240)
            new = pool.ensure('office', 'rtsp://fixture/new')
            self.assertTrue(old.stopped.is_set())
            self.assertIsNot(new, old)
            self.assertIsNone(new.frame())
            pool.close()

    def test_regular_lease_removal_stops_jpeg_but_preserves_observation_reader(self):
        with patch.object(LiveFrameReader, 'start'):
            pool = LiveFramePool()
            pool.sync({'office': 'rtsp://fixture/office'})
            reader = pool.acquire_observation('office', 'rtsp://fixture/office')
            self.assertTrue(reader.encode_jpeg)
            pool.sync({})
            self.assertFalse(reader.encode_jpeg)
            self.assertFalse(reader.stopped.is_set())
            self.assertTrue(pool.report()['office']['observation_consumer'])
            self.assertFalse(pool.report()['office']['regular_consumer'])
            pool.sync({'office': 'rtsp://fixture/office'})
            self.assertTrue(reader.encode_jpeg)
            pool.release_observation('office')
            self.assertFalse(reader.stopped.is_set())
            pool.sync({})
            self.assertTrue(reader.stopped.is_set())

    def test_frame_detail_check_is_demanded_cached_and_outside_decoder_lock(self):
        reader = LiveFrameReader('rtsp://fixture/office')
        reader.state = 'streaming'
        reader.decoded = {'sequence': 1, 'generation': 0, 'arrived': 10,
                          'image': np.zeros((80, 80, 3), np.uint8)}
        def detail(image):
            self.assertTrue(reader.lock.acquire(blocking=False))
            reader.lock.release()
            return {'detail': 'structured'}
        with patch('app.live_frames.time.monotonic', return_value=10.1), \
                patch('app.live_frames.frame_detail', side_effect=detail) as check:
            self.assertEqual(reader.frame_record()['sequence'], 1)
            self.assertEqual(reader.frame_record()['sequence'], 1)
            check.assert_called_once()

    def test_flat_frame_and_generation_change_during_detail_check_are_rejected(self):
        reader = LiveFrameReader('rtsp://fixture/office')
        reader.state = 'streaming'
        reader.decoded = {'sequence': 1, 'generation': 0, 'arrived': 10,
                          'image': np.zeros((80, 80, 3), np.uint8)}
        with patch('app.live_frames.time.monotonic', return_value=10.1):
            self.assertIsNone(reader.frame_record())
            reader.decoded.pop('_structured')
            def reconnect(image):
                reader.generation += 1
                return {'detail': 'structured'}
            with patch('app.live_frames.frame_detail', side_effect=reconnect):
                self.assertIsNone(reader.frame_record())

    def test_reader_drains_frames_and_releases_capture_on_stop(self):
        reader = LiveFrameReader('rtsp://fixture/office')
        capture = Mock()
        capture.isOpened.return_value = True
        frames = iter([np.zeros((60, 80, 3), np.uint8), np.ones((60, 80, 3), np.uint8)])
        def read():
            try: return True, next(frames)
            except StopIteration:
                reader.stop()
                return False, None
        capture.read.side_effect = read
        with patch('app.live_frames.cv2.VideoCapture', return_value=capture) as factory:
            reader.run()
        capture.release.assert_called_once()
        self.assertEqual(capture.read.call_count, 3)
        self.assertIn(3000, factory.call_args.args[2])
        self.assertIn(1000, factory.call_args.args[2])

    def test_failed_capture_clears_old_evidence_and_does_not_expose_url(self):
        reader = LiveFrameReader('rtsp://fixture/private-stream')
        reader.publish(b'old', 320, 240)
        capture = Mock(isOpened=Mock(return_value=False))
        reader.stopped.wait = Mock(side_effect=lambda seconds: reader.stop() or True)
        with patch('app.live_frames.cv2.VideoCapture', return_value=capture):
            reader.run()
        self.assertIsNone(reader.frame())
        self.assertNotIn('private-stream', str(reader.report()))
        capture.release.assert_called_once()

class StreamClockOriginTests(unittest.TestCase):
    def test_negative_and_zero_stream_origins_are_valid_when_progressing(self):
        for origin in (-100000,0,100000):
            reader=LiveFrameReader('private-fixture')
            self.assertFalse(reader.accept_position(origin,10,10))
            self.assertTrue(reader.accept_position(origin+1000,11,10))
            self.assertTrue(reader.accept_position(origin+1040,11.04,10))
            self.assertFalse(reader.accept_position(origin+1040,11.08,10))
            self.assertEqual(reader.report()['rejection_reason'],'timestamp_nonprogressing')
    def test_growth_and_invalid_clock_remain_rejected(self):
        reader=LiveFrameReader('private-fixture')
        self.assertFalse(reader.accept_position(float('nan'),10,10))
        self.assertFalse(reader.accept_position(-10000,10,10))
        self.assertTrue(reader.accept_position(-9000,11,10))
        self.assertFalse(reader.accept_position(-8960,13,10))
        self.assertEqual(reader.report()['rejection_reason'],'buffering_growth')

class DecoderTimestampTests(unittest.TestCase):
    def test_frame_position_preserves_media_precision_and_negative_origin(self):
        import cv2
        from app.live_frames import decoder_position_ms
        capture=Mock(get=Mock(side_effect=[-2001.1, -1962.1, float('nan')]))
        self.assertEqual(decoder_position_ms(capture),-2001.1)
        self.assertEqual(decoder_position_ms(capture),-1962.1)
        self.assertTrue(__import__('math').isnan(decoder_position_ms(capture)))
        self.assertTrue(all(call.args == (cv2.CAP_PROP_POS_MSEC,) for call in capture.get.call_args_list))
