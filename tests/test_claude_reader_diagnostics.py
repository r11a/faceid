"""6.8.4 diagnostics: stream reader reopens, decoder lag and read wait, container CPU."""
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from app import recognition_capture as capture_module
from app.live_frames import LiveFramePool, LiveFrameReader
from tests.test_recognition_capture import processor_fixture


def scripted_capture(positions):
    capture = Mock()
    capture.isOpened.return_value = True
    frames = iter(positions)

    def read():
        try:
            capture.position = next(frames)
            return True, np.zeros((8, 8, 3), np.uint8)
        except StopIteration:
            return False, None
    capture.read.side_effect = read
    capture.get.side_effect = lambda _: capture.position
    return capture


class ReaderReopenTests(unittest.TestCase):
    def run_once(self, capture, clock_step=0.0):
        reader = LiveFrameReader('rtsp://fixture/private-stream')
        reader.encode_jpeg = False
        reader.stopped.wait = Mock(side_effect=lambda seconds: reader.stop() or True)
        clock = [100.0]

        def monotonic():
            clock[0] += clock_step
            return clock[0]
        with patch('app.live_frames.open_capture', return_value=capture), \
                patch('app.live_frames.time.monotonic', side_effect=monotonic), \
                self.assertLogs('app.live_frames', 'INFO') as logs:
            reader.run()
        return reader, '\n'.join(logs.output)

    def test_ended_stream_is_recorded_and_logged_with_its_reason(self):
        reader, log = self.run_once(scripted_capture([]))
        report = reader.report()
        self.assertEqual(report['reopens'], 1)
        self.assertEqual(report['reopen_reasons'], {'read_failed': 1})
        self.assertEqual(report['last_reopen']['reason'], 'read_failed')
        self.assertIn('Stream reader reopening: reason=read_failed', log)
        self.assertNotIn('private-stream', log + str(report))

    def test_decoder_falling_behind_reopens_as_catch_up_timeout_with_its_lag(self):
        # Media time advances 20 ms per frame while each loop takes longer:
        # the lag behind the stream clock grows until the reader gives up.
        reader, log = self.run_once(scripted_capture([i * 20 for i in range(400)]), clock_step=0.02)
        last = reader.report()['last_reopen']
        self.assertEqual(last['reason'], 'catch_up_timeout')
        self.assertGreater(last['lag_ms'], 750)
        self.assertIsNotNone(last['read_wait_ms'])
        self.assertEqual(last['instant_read_pct'], 0.0)
        self.assertIn('reason=buffering_growth lag_ms=', log)
        self.assertIn('reason=catch_up_timeout', log)

    def test_a_stop_is_not_counted_as_a_reopen(self):
        reader = LiveFrameReader('rtsp://fixture/office')
        capture = scripted_capture([])
        capture.read.side_effect = lambda: (reader.stop(), (False, None))[1]
        with patch('app.live_frames.open_capture', return_value=capture):
            reader.run()
        self.assertEqual(reader.report()['reopens'], 0)

    def test_first_usable_frame_after_a_reopen_is_recorded_once(self):
        reader = LiveFrameReader('rtsp://fixture/office')
        reader._note_first_usable(0.3)  # first connection: not a reopen
        self.assertIsNone(reader.report()['last_reopen'])
        reader.generation = 4
        with self.assertLogs('app.live_frames', 'INFO'):
            reader._note_reopen('catch_up_timeout', 61.0, 1.0)
        reader.generation = 6
        with self.assertLogs('app.live_frames', 'INFO') as logs:
            reader._note_first_usable(0.42)
        reader._note_first_usable(0.9)
        last = reader.report()['last_reopen']
        self.assertEqual((last['generation_before'], last['first_usable_ms'], last['generation_after']), (4, 420, 6))
        self.assertIn('first usable frame after 420 ms', logs.output[0])

    def test_instant_reads_are_counted(self):
        reader = LiveFrameReader('rtsp://fixture/office')
        for wait in (0.001, 0.002, 0.12, 0.12):
            reader.note_read(wait)
        self.assertEqual(reader.read_stats()['instant_read_pct'], 50.0)


class CaptureReaderTests(unittest.TestCase):
    def setUp(self):
        self.processor, _ = processor_fixture()
        self.capture = self.processor.recognition_capture
        self.clock = [100.0]
        for target, effect in [('time.monotonic', lambda: self.clock[0]),
                               ('time.process_time', lambda: 50.0)]:
            patcher = patch('app.recognition_capture.' + target, side_effect=effect)
            patcher.start(); self.addCleanup(patcher.stop)
        patcher = patch.object(self.capture, '_run')
        patcher.start(); self.addCleanup(patcher.stop)
        with patch.object(LiveFrameReader, 'start'):
            self.pool = LiveFramePool()
            self.reader = self.pool.ensure('front', 'rtsp://fixture/front')
        self.processor.frigate.live_frames = self.pool
        self.cpu_usec = [1_000_000]
        patcher = patch('app.recognition_capture._container_cpu_usec', side_effect=lambda: self.cpu_usec[0])
        patcher.start(); self.addCleanup(patcher.stop)
        self.capture.start('admin', 'front', 'frigate_restream', 300)

    def tick(self, seconds=1.0, cpu_usec=0):
        self.clock[0] += seconds
        self.cpu_usec[0] += cpu_usec
        self.capture._tick()

    def test_samples_carry_container_cpu_and_reader_state(self):
        self.tick()
        self.reader.lag_ms = 180
        self.tick(cpu_usec=250_000)
        sample = self.capture.report('admin')['resources'][-1]
        self.assertEqual(sample['container_cpu_percent_one_core'], 25.0)
        self.assertEqual(sample['reader']['lag_ms'], 180)
        self.assertEqual(set(sample['reader']), set(capture_module.READER_SAMPLE))

    def test_reopens_between_events_become_run_records(self):
        self.tick()
        self.reader.generation = 7
        with self.assertLogs('app.live_frames', 'INFO'):
            self.reader._note_reopen('catch_up_timeout', 60.4, 1.0)
        self.tick()
        self.reader.generation = 9
        with self.assertLogs('app.live_frames', 'INFO'):
            self.reader._note_first_usable(0.5)
        self.tick()
        self.tick()
        report = self.capture.report('admin')
        stages = [record['stage'] for record in report['records']]
        self.assertEqual(stages, ['reader_started', 'reader_reopen', 'reader_reopened'])
        reopen = report['records'][1]
        self.assertEqual((reopen['reason'], reopen['connected_s'], reopen['generation_before']),
                         ('catch_up_timeout', 60.4, 7))
        self.assertEqual(report['records'][2]['first_usable_ms'], 500)
        self.assertNotIn('reader_seen', report)
        self.assertNotIn('last_container_cpu', report)


class ContainerCpuTests(unittest.TestCase):
    def test_cgroup_v2_usage_and_v1_fallback(self):
        files = {'/sys/fs/cgroup/cpu.stat': 'usage_usec 123456\nuser_usec 1\n'}

        def read_text(path):
            if str(path).replace('\\', '/') in files:
                return files[str(path).replace('\\', '/')]
            raise OSError
        with patch.object(capture_module.Path, 'read_text', autospec=True, side_effect=read_text):
            self.assertEqual(capture_module._container_cpu_usec(), 123456)
            files.clear()
            files['/sys/fs/cgroup/cpuacct/cpuacct.usage'] = '5000000\n'
            self.assertEqual(capture_module._container_cpu_usec(), 5000)
            files.clear()
            self.assertIsNone(capture_module._container_cpu_usec())


if __name__ == '__main__':
    unittest.main()
