"""Bounded, non-authorizing diagnostic comparison for the configured restream."""
import math
import json
import subprocess
import threading
import time

import cv2

from .decode_diagnostics import DecodeReader, run_decode
from .live_frames import LiveFrameReader


BASELINE_SECONDS = 12
DECODE_SECONDS = 45
TRACE_LIMIT = 2048


def finite(value):
    return value if math.isfinite(value) else None


class TimestampTrace:
    def __init__(self, publish, phase_changed=lambda _: None):
        self.publish = publish
        self.phase_changed = phase_changed
        self.phase = None
        self.current = LiveFrameReader(None)
        self.alternate = LiveFrameReader(None)
        self.rows = []
        self.seen = 0
        self.preview_at = -float('inf')

    def __call__(self, capture, frame, selected, now, opened, read_seconds):
        self.seen += 1
        fps = finite(capture.get(cv2.CAP_PROP_FPS))
        pts_property = getattr(cv2, 'CAP_PROP_PTS', None)
        pts_units = finite(capture.get(pts_property)) if pts_property is not None else None
        position = capture.get(cv2.CAP_PROP_POS_MSEC)
        accepted = self.current.accept_position(selected, now, opened)
        coarse = pts_units*1000/fps if pts_units is not None and fps is not None and 0<fps<=1000 else None
        alternate_ok = self.alternate.accept_position(coarse, now, opened)
        baseline = now-opened < BASELINE_SECONDS
        phase = 'baseline' if baseline else 'recognition_load'
        if phase != self.phase:
            self.phase = phase
            self.phase_changed('timestamp_' + phase)
        reason = None if accepted else self.current.clock_reason
        growth_ms = None
        if math.isfinite(selected) and self.current.clock_offset is not None:
            growth_ms = round((now-selected/1000-self.current.clock_offset)*1000, 3)
        preview_ms = None
        if now-self.preview_at >= 1:
            self.preview_at = now
            start = time.monotonic()
            scale = min(1., 960/frame.shape[1])
            self.publish({'scene': cv2.resize(frame, None, fx=scale, fy=scale)})
            preview_ms = round((time.monotonic()-start)*1000, 2)
        if len(self.rows) < TRACE_LIMIT:
            self.rows.append([self.seen, phase, round((now-opened)*1000, 3),
                round(read_seconds*1000, 3), fps, pts_units, finite(position), finite(selected),
                reason, None if alternate_ok else self.alternate.clock_reason,
                self.current.generation, preview_ms, growth_ms])
        # Alternate time is recorded only. The actual recognition experiment
        # uses the production verifier and never counts a rejected frame.
        return ('baseline_no_inference' if baseline else reason), self.current.generation

    def report(self):
        return {'columns': ['sequence','phase','since_open_ms','read_ms','fps','opencv_pts_units',
                'opencv_pos_msec','selected_timestamp_ms','production_rejection',
                'legacy_fps_rejection','production_generation','preview_encode_ms','selected_clock_growth_ms'],
                'frames_seen': self.seen, 'frames_omitted': self.seen-len(self.rows),
                'rows': self.rows, 'baseline_seconds_after_open': BASELINE_SECONDS,
                'verifier_behavior': 'Production accept_position on every frame. The diagnostic keeps the connection open to observe recovery; production may reconnect.'}


def native_reference(url):
    """Independent bounded FFmpeg run. Read timestamps in presentation order."""
    cmd = ['ffprobe','-v','error','-rtsp_transport','tcp','-select_streams','v:0',
           '-read_intervals','%+#300','-show_frames','-show_entries',
           'frame=pts_time,best_effort_timestamp_time','-of','json',url]
    try:
        output = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=18, check=False)
        frames = json.loads(output.stdout).get('frames', [])[:300]
        return {'connection':'separate_sequential','exit_code':output.returncode,
                'frames':[ {key:finite(float(frame[key])) for key in ('pts_time','best_effort_timestamp_time')
                            if key in frame and frame[key] != 'N/A'} for frame in frames]}
    except subprocess.TimeoutExpired:
        return {'error':'native_reference_timeout'}
    except (OSError, ValueError):
        return {'error':'native_reference_unavailable'}


def run_timestamp_test(processor, camera, url, publish, probe, phase):
    begin = time.monotonic()
    packet = {}
    worker = None

    def packet_reference():
        packet['started_ms'] = round((time.monotonic()-begin)*1000, 2)
        try:
            packet['result'] = probe(url, seconds=12)
        except Exception:
            packet['result'] = {'error':'packet_reference_failed'}
        finally:
            packet['finished_ms'] = round((time.monotonic()-begin)*1000, 2)

    def phase_changed(value):
        nonlocal worker
        phase(value)
        if value == 'timestamp_recognition_load':
            worker = threading.Thread(target=packet_reference, daemon=True, name='timestamp-packet-reference')
            worker.start()

    trace = TimestampTrace(publish, phase_changed)
    phase('timestamp_connecting')
    recognition = run_decode(processor, camera, url, seconds=DECODE_SECONDS,
        reader_factory=lambda source, seconds: DecodeReader(source, seconds, observer=trace))
    report = {'mode':'timestamp_comparison','recognition':recognition,'opencv':trace.report(),
              'opencv_version':cv2.__version__, 'timestamp_source':'opencv_pos_msec',
              'manual_access1_submission':'not_implemented',
              'operational_access1_submission':'normal_saved_policy',
              'frames_saved':0, 'freshness_proven':False,
              'reference_note':'Both OpenCV readings describe the same decoded frame. The packet probe overlaps recognition on a separate RTSP connection. FFmpeg runs afterward. Compare cadence and stalls; independent connections do not establish exact frame correspondence or capture age.'}
    if worker is not None:
        phase('timestamp_packet_reference')
        worker.join(30)
    report['rtp_overlap'] = dict(packet) if worker is not None and not worker.is_alive() else {
        'error':'packet_reference_still_running' if worker is not None else 'recognition_phase_not_reached'}
    if recognition.get('decoder', {}).get('decoder_stopped') and (worker is None or not worker.is_alive()):
        phase('timestamp_native_reference')
        report['native_ffmpeg'] = native_reference(url)
    return report
