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
