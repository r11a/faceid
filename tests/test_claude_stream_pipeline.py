"""6.8.0: GPU stream source, CPU fallback, background audit writer, chained stream steps."""
import io
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

import app.live_frames as live_frames
from app.audit import AuditStore
from app.mqtt_listener import EventProcessor
from app.vaapi_source import VaapiCapture, command, parse_showinfo

LINE = ('[Parsed_showinfo_4 @ 0x5581] n:   {n} pts:  {pts} pts_time:{time} duration:1 '
        'pos:-1 fmt:nv12 sar:1/1 s:{w}x{h} i:P iskey:0 type:P\n')


class FakeProcess:
    def __init__(self, frames, width=4, height=3, exit_code=None):
        lines = ''.join(LINE.format(n=i, pts=i * 100, time=i / 10, w=width, h=height)
                        for i in range(frames))
        self.stderr = io.BytesIO(('Input #0, rtsp, from secret-url\n' + lines).encode())
        self.stdout = io.BytesIO(bytes(range(256)) * (frames * width * height * 3 // 256 + 1))
        self.killed = False
        self.exit_code = exit_code

    def poll(self):
        return self.exit_code

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return 0


class VaapiSourceTests(unittest.TestCase):
    def test_showinfo_lines_give_media_time_and_size_only(self):
        self.assertEqual(parse_showinfo(LINE.format(n=3, pts=9000, time='0.3', w=1920, h=1440)),
                         (300.0, 1920, 1440))
        self.assertIsNone(parse_showinfo("Input #0, rtsp, from 'rtsp://user:pass@cam'"))
        self.assertIsNone(parse_showinfo(LINE.format(n=0, pts=0, time=0, w=0, h=10)))

    def test_command_decodes_and_scales_on_the_gpu_and_keeps_aspect(self):
        cmd = command('rtsp://frigate:8554/door', width=1920, step=3, device='/dev/dri/renderD128')
        chain = cmd[cmd.index('-vf') + 1]
        self.assertIn('-hwaccel', cmd)
        self.assertEqual(chain, 'scale_vaapi=w=1920:h=-2,hwdownload,format=nv12,framestep=3,showinfo=checksum=0')
        self.assertEqual(cmd[cmd.index('-vsync') + 1], 'passthrough')
        self.assertLess(cmd.index('-vf'), cmd.index('-vsync'))
        self.assertIn('-nostats', cmd)
        self.assertEqual(cmd[cmd.index('-extra_hw_frames') + 1], '8')
        native = command('u', width=0, step=3, device='d')
        self.assertEqual(native[native.index('-vf') + 1], 'hwdownload,format=nv12,framestep=3,showinfo=checksum=0')
        plain = command('u', width=0, step=3, device='d', checksum_option=False)
        self.assertEqual(plain[plain.index('-vf') + 1], 'hwdownload,format=nv12,framestep=3,showinfo')

    def test_frames_arrive_as_bgr_with_their_media_time(self):
        process = FakeProcess(frames=2)
        capture = VaapiCapture('rtsp://x', popen=lambda *a, **k: process)
        ok, frame = capture.read()
        self.assertTrue(ok)
        self.assertEqual(frame.shape, (3, 4, 3))
        self.assertTrue(frame.flags.writeable)
        self.assertEqual(capture.get(0), 0.0)
        ok, _ = capture.read()
        self.assertTrue(ok)
        self.assertEqual(capture.get(0), 100.0)
        self.assertEqual(capture.frames, 2)
        capture.release()
        self.assertTrue(process.killed)

    def test_a_stream_that_stops_mid_way_is_reported_as_stalled(self):
        import app.vaapi_source as source
        process = FakeProcess(frames=1)
        process.stderr = io.BytesIO(LINE.format(n=0, pts=0, time=0, w=4, h=3).encode())
        with patch.object(source, 'READ_TIMEOUT', .05):
            capture = VaapiCapture('rtsp://x', popen=lambda *a, **k: process)
            capture._thread.join(1)
            while not capture._infos.empty():  # Simulate silence: no end-of-stream marker.
                capture._infos.get_nowait()
            capture._infos.put((0.0, 4, 3))
            self.assertTrue(capture.read()[0])
            self.assertEqual(capture.read(), (False, None))
        self.assertTrue(capture.stalled)

    def test_ended_stream_reports_failure(self):
        capture = VaapiCapture('rtsp://x', popen=lambda *a, **k: FakeProcess(frames=0))
        self.assertEqual(capture.read(), (False, None))
        self.assertFalse(capture.isOpened())


class GpuFallbackTests(unittest.TestCase):
    def setUp(self):
        self.saved = dict(live_frames._gpu)
        self.addCleanup(live_frames._gpu.update, self.saved)
        live_frames._gpu.update(failures=0, retry_at=0.0, checksum=None, active_logged=False)

    def test_gpu_source_is_used_and_alternates_the_showinfo_variant(self):
        with patch.object(live_frames, 'GPU_DECODE', True),                 patch.object(live_frames.os.path, 'exists', return_value=True),                 patch.object(live_frames, 'VaapiCapture') as gpu,                 patch.object(live_frames.cv2, 'VideoCapture') as cpu:
            live_frames.open_capture('rtsp://x')
            live_frames._gpu['failures'] = 1
            live_frames.open_capture('rtsp://x')
        self.assertEqual([c.kwargs['checksum_option'] for c in gpu.call_args_list], [True, False])
        cpu.assert_not_called()

    def test_repeated_gpu_failures_fall_back_to_cpu_for_a_while(self):
        failed = SimpleNamespace(frames=0, checksum_option=True)
        with patch.object(live_frames, 'VaapiCapture', SimpleNamespace):
            for _ in range(3):
                live_frames._gpu_result(failed)
        self.assertGreater(live_frames._gpu['retry_at'], time.monotonic() + 60)
        with patch.object(live_frames, 'GPU_DECODE', True),                 patch.object(live_frames.os.path, 'exists', return_value=True),                 patch.object(live_frames, 'VaapiCapture') as gpu,                 patch.object(live_frames.cv2, 'VideoCapture') as cpu:
            live_frames.open_capture('rtsp://x')
        gpu.assert_not_called()
        cpu.assert_called_once()

    def test_success_keeps_gpu_and_remembers_the_working_command(self):
        ok = SimpleNamespace(frames=5, checksum_option=False)
        with patch.object(live_frames, 'VaapiCapture', SimpleNamespace):
            live_frames._gpu_result(ok)
        self.assertEqual(live_frames._gpu['failures'], 0)
        self.assertIs(live_frames._gpu['checksum'], False)

    def test_cpu_decoder_is_used_when_gpu_decode_is_off(self):
        with patch.object(live_frames, 'GPU_DECODE', False), \
                patch.object(live_frames, 'VaapiCapture') as gpu, \
                patch.object(live_frames.cv2, 'VideoCapture') as cpu:
            live_frames.open_capture('rtsp://x')
        gpu.assert_not_called()
        cpu.assert_called_once()


class AuditWriterTests(unittest.TestCase):
    def processor(self, root):
        processor = EventProcessor.__new__(EventProcessor)
        processor.audit = AuditStore(Path(root) / 'audit.db')
        processor.audit_async = True
        self.addCleanup(lambda: processor.__dict__.get('_audit_executor') and
                        processor._audit_executor.shutdown(wait=True))
        return processor

    def test_queued_writes_keep_order_and_sample_time_and_flush_waits(self):
        with tempfile.TemporaryDirectory() as root:
            processor = self.processor(root)
            order, gate = [], threading.Event()
            processor._audit_later(lambda: (gate.wait(2), order.append(1)))
            processor._audit_later(order.append, 2)
            self.assertEqual(order, [])  # The caller did not wait for the writes.
            gate.set()
            processor._audit_flush()
            self.assertEqual(order, [1, 2])
            processor._audit_later(processor.audit.observation, 'e1', 1, 'liveness_pending', ts=123.0)
            processor._audit_flush()
            with processor.audit._connection() as con:
                self.assertEqual(con.execute('SELECT ts FROM observations').fetchone()[0], 123.0)
            processor._audit_executor.shutdown(wait=True)

    def test_slow_pending_write_cannot_overwrite_final_evidence(self):
        processor = EventProcessor.__new__(EventProcessor)
        processor.audit_async = True
        gate, finished = threading.Event(), threading.Event()
        writes = []
        processor._audit_later(lambda: (gate.wait(5), writes.append("pending")))
        def finalize():
            processor._audit_flush(timeout=.01)
            writes.append("final")
            finished.set()
        thread = threading.Thread(target=finalize)
        thread.start()
        try:
            self.assertFalse(finished.wait(.1))
        finally:
            gate.set()
            thread.join(5)
            processor._audit_executor.shutdown(wait=True)
        self.assertEqual(writes, ["pending", "final"])
        self.assertTrue(finished.is_set())

    def test_full_audit_queue_applies_backpressure_and_preserves_order(self):
        processor = EventProcessor.__new__(EventProcessor)
        processor.audit_async = True
        gate, submitted = threading.Event(), threading.Event()
        writes = []
        with patch("app.mqtt_listener.AUDIT_QUEUE_LIMIT", 2):
            processor._audit_later(lambda: (gate.wait(5), writes.append(1)))
            processor._audit_later(writes.append, 2)
            def submit():
                processor._audit_later(writes.append, 3)
                submitted.set()
            thread = threading.Thread(target=submit)
            thread.start()
            try:
                self.assertFalse(submitted.wait(.1))
            finally:
                gate.set()
                thread.join(5)
                processor._audit_flush()
                processor._audit_executor.shutdown(wait=True)
        self.assertEqual(writes, [1, 2, 3])
        self.assertTrue(submitted.is_set())

    def test_inline_without_the_option(self):
        processor = EventProcessor.__new__(EventProcessor)
        calls = []
        processor._audit_later(calls.append, 1)
        self.assertEqual(calls, [1])


class ChainedStepTests(unittest.TestCase):
    def test_a_step_that_used_a_frame_schedules_the_next_at_once(self):
        from app.stream_observation import StreamObservations
        processor = SimpleNamespace(engine=None)
        streams = StreamObservations(processor)
        self.addCleanup(streams.executor.shutdown, wait=True)
        streams.executor = Mock()
        streams.sessions['e'] = {'camera': 'door', 'st': {}}
        streams._step = lambda camera: streams.counters.__setitem__(
            'frames_admitted', streams.counters['frames_admitted'] + 1)
        streams._work('door')
        streams.executor.submit.assert_called_once_with(streams._work, 'door')
        streams.executor.submit.reset_mock()
        streams._step = lambda camera: None  # No new frame: wait for the regular tick.
        streams._work('door')
        streams.executor.submit.assert_not_called()


if __name__ == '__main__':
    unittest.main()
