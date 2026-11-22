"""Bounded persistent restream readers; inference consumes only the latest frame."""
import threading
import time
import math
import logging
from collections import Counter, deque
_LOGGER=logging.getLogger(__name__)
# FFmpeg decoder threads per reader. One thread fell slowly behind a full
# resolution Entrance stream on the N100 (~15 ms of lag per second).
DECODE_THREADS = 2
import os
# Native decoder errors can contain credential-bearing URLs. Python diagnostics
# remain enabled and report categorical acquisition failures instead.
os.environ['OPENCV_FFMPEG_LOGLEVEL']='-8'
os.environ['OPENCV_LOG_LEVEL']='SILENT'

import cv2
cv2.setLogLevel(0)
from .preview_startup import frame_detail
from .vaapi_source import VaapiCapture

# GPU decoding through the system FFmpeg (main.py enables it from the option).
# Frames are scaled on the GPU to DECODE_WIDTH (same aspect); every DECODE_STEP-th
# decoded frame is delivered (about 8 fps from a 25 fps camera).
GPU_DECODE = False
DECODE_WIDTH = 0  # native size: far faces keep every pixel for identification
DECODE_STEP = 3
# 6.9.0: optional per-camera (gpu, width, step) from the camera profile; a camera
# without an entry uses the global values above (the 6.8.7 behaviour).
CAMERA_DECODE = {}
INSTANT_READ_MS = 10  # a read this fast was already waiting in the decoder
VAAPI_DEVICE = '/dev/dri/renderD128'
GPU_RETRY_SECONDS = 600
_gpu = {'failures': 0, 'retry_at': 0.0, 'checksum': None, 'active_logged': False, 'stall_logged': 0.0}
_gpu_lock = threading.Lock()


def open_capture(url, decode=None):
    """GPU source when enabled and not recently failed; else the OpenCV CPU decoder."""
    gpu, width, step = (GPU_DECODE, DECODE_WIDTH, DECODE_STEP) if decode is None else decode
    with _gpu_lock:
        use_gpu = gpu and time.monotonic() >= _gpu['retry_at'] and os.path.exists(VAAPI_DEVICE)
        # Some FFmpeg builds lack showinfo's checksum option: alternate until one works.
        checksum = _gpu['checksum'] if _gpu['checksum'] is not None else _gpu['failures'] % 2 == 0
    if use_gpu:
        try:
            return VaapiCapture(url, width=width, step=step, device=VAAPI_DEVICE,
                                checksum_option=checksum)
        except OSError:
            _gpu_result(None)
    return cv2.VideoCapture(url, cv2.CAP_FFMPEG, [
        cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 3000,
        cv2.CAP_PROP_READ_TIMEOUT_MSEC, 1000,
        cv2.CAP_PROP_N_THREADS, DECODE_THREADS,
    ])


def _gpu_started(capture):
    """Log once that GPU decoding delivers frames."""
    with _gpu_lock:
        if _gpu['active_logged']:
            return
        _gpu['active_logged'] = True
    width = getattr(capture, 'decode_width', DECODE_WIDTH)
    step = getattr(capture, 'decode_step', DECODE_STEP)
    _LOGGER.info('Stream reader: GPU (VAAPI) decode active, %s, every %s. frame',
                 f'{width} px wide' if width else 'native size', step)


def _gpu_result(capture):
    """Record whether a GPU source delivered frames; fall back after repeated failures."""
    if capture is not None and not isinstance(capture, VaapiCapture):
        return
    with _gpu_lock:
        if capture is not None and capture.frames:
            _gpu['failures'] = 0
            _gpu['checksum'] = capture.checksum_option
            if getattr(capture, 'stalled', False) and time.monotonic() - _gpu['stall_logged'] >= 60:
                _gpu['stall_logged'] = time.monotonic()
                _LOGGER.info('Stream reader: GPU stream stalled after %d frames; reconnecting',
                             capture.frames)
            return
        _gpu['failures'] += 1
        if _gpu['failures'] >= 3:
            _gpu['failures'] = 0
            _gpu['retry_at'] = time.monotonic() + GPU_RETRY_SECONDS
            _gpu['active_logged'] = False
            _LOGGER.warning('Stream reader: GPU decode unavailable; using CPU decode for %s s',
                            GPU_RETRY_SECONDS)


def decoder_position_ms(capture):
    """Read FFmpeg's frame position without rounding to nominal FPS ticks."""
    # This is media time, not camera wall time. Do not switch clocks or
    # synthesize a timestamp when the decoder reports missing or stale data.
    return capture.get(cv2.CAP_PROP_POS_MSEC)


class LiveFrameReader:
    def __init__(self, url, *, max_age=1.0, decode=None):
        self.url = url
        self.decode = decode
        self.max_age = max_age
        self.stopped = threading.Event()
        self.lock = threading.Lock()
        self.thread = None
        self.latest = None
        self.received = 0.0
        self.state = "connecting"
        self.width = self.height = self.frames = 0
        self.clock_offset = None
        self.last_position = None
        self.generation = 0
        self.sequence = 0
        self.decoded = None
        self.position_arrived = None
        self.invalid_clock = False
        self.clock_reason = None
        self.last_rejection_log = -float("inf")
        self.rejections = Counter()
        self.timestamp_origin = None
        self.encode_jpeg = True
        # Diagnostics only: why and how often the stream is reopened, the
        # decoder lag behind the stream clock, and how long reads block.
        # Reads that return at once while the lag grows mean a real backlog;
        # reads blocking a frame interval mean the decoder keeps up.
        self.reopens = 0
        self.reopen_reasons = Counter()
        self.last_reopen = None
        self.lag_ms = None
        self.read_wait_ms = None
        self.reads = self.instant_reads = 0
        self.min_offset = None
        self.last_read_live = False
        self.drift_ms = None

    def note_read(self, wait):
        """Track decoder read blocking time (EWMA) and reads that returned at once."""
        ms = wait * 1000
        self.reads += 1
        self.last_read_live = ms >= INSTANT_READ_MS
        self.instant_reads += not self.last_read_live
        self.read_wait_ms = ms if self.read_wait_ms is None else self.read_wait_ms * 0.9 + ms * 0.1

    def read_stats(self):
        return {'lag_ms': self.lag_ms, 'drift_ms': self.drift_ms,
                'read_wait_ms': round(self.read_wait_ms, 1) if self.read_wait_ms is not None else None,
                'instant_read_pct': round(100 * self.instant_reads / self.reads, 1) if self.reads else None}

    def accept_position(self, position_ms, now, started):
        """Reject decoder backlog using stream timestamps, not JPEG receipt age."""
        if not isinstance(position_ms, (int, float)) or not math.isfinite(position_ms) :
            self.invalidate_clock("timestamp_missing")
            return False
        position = position_ms / 1000
        if self.last_position is not None and (position <= self.last_position or
                (self.position_arrived is not None and position-self.last_position > max(2,now-self.position_arrived+.75))):
            self.invalidate_clock("timestamp_nonprogressing" if position <= self.last_position else "timestamp_forward_jump")
            return False
        self.last_position = position
        self.position_arrived = now
        offset = now - position
        self.min_offset = offset if self.min_offset is None else min(self.min_offset, offset)
        # A blocking read includes decoder work and cannot prove the live edge.
        # Keep the connection baseline fixed so gradual backlog cannot be hidden.
        self.clock_offset = self.min_offset
        self.drift_ms = round((self.clock_offset - self.min_offset) * 1000)
        self.lag_ms = round((offset - self.clock_offset) * 1000)
        valid=now-started>=.5 and offset-self.clock_offset<=.75
        if not valid:self.invalidate_clock("startup_draining" if now-started<.5 else "buffering_growth")
        else:self.invalid_clock=False
        return valid

    def invalidate_clock(self, reason="timestamp_invalid"):
        self.clock_reason=reason
        self.rejections[reason]+=1
        if not self.invalid_clock and reason!="startup_draining" and time.monotonic()-self.last_rejection_log>=5:
            self.last_rejection_log=time.monotonic()
            stats=self.read_stats()
            _LOGGER.info("Stream reader rejected decoded frame: reason=%s lag_ms=%s read_wait_ms=%s instant_reads=%s%%",
                         reason,stats['lag_ms'],stats['read_wait_ms'],stats['instant_read_pct'])
        if not self.invalid_clock:
            with self.lock:
                self.generation+=1;self.decoded=None
            self.invalid_clock=True

    def start(self):
        self.thread = threading.Thread(target=self.run, daemon=True, name="faceid-live-frame")
        self.thread.start()

    def publish(self, content, width, height):
        with self.lock:
            if self.stopped.is_set():
                return
            self.latest, self.received = content, time.monotonic()
            self.width, self.height = width, height
            self.frames += 1
            self.state = "streaming"

    def frame(self):
        with self.lock:
            if self.stopped.is_set() or self.state != "streaming" or time.monotonic() - self.received > self.max_age:
                return None
            return self.latest

    def frame_record(self):
        """Internal bounded BGR evidence, never a capture-time claim."""
        with self.lock:
            if self.stopped.is_set() or self.state != 'streaming' or self.decoded is None:
                return None
            if not 0 <= time.monotonic()-self.decoded['arrived'] <= self.max_age:
                return None
            record = dict(self.decoded)
        # Check only frames actually requested by inference, outside the reader
        # lock. The decoder keeps draining; JPEG-only consumers need no check.
        structured = record.pop('_structured', None)
        if structured is None:
            structured = frame_detail(record['image'])['detail'] == 'structured'
        with self.lock:
            if (self.stopped.is_set() or self.state != 'streaming' or
                    self.generation != record['generation'] or
                    not 0 <= time.monotonic()-record['arrived'] <= self.max_age):
                return None
            if self.decoded is not None and self.decoded['sequence'] == record['sequence']:
                self.decoded['_structured'] = structured
        return record if structured else None

    def report(self):
        with self.lock:
            age = time.monotonic() - self.received if self.received else None
            state = "stopped" if self.stopped.is_set() else self.state
            if state == "streaming" and age is not None and age > self.max_age:
                state = "stale"
            return {"state": state, "width": self.width, "height": self.height,
                    "frames": self.frames, "generation":self.generation, "rejection_reason":self.clock_reason, "rejections":dict(self.rejections), "age_ms": round(age * 1000) if age is not None else None,
                    "jpeg_enabled":self.encode_jpeg, "reopens":self.reopens,
                    "reopen_reasons":dict(self.reopen_reasons),
                    "last_reopen":dict(self.last_reopen) if self.last_reopen else None,
                    **self.read_stats()}

    def stop(self):
        self.stopped.set()
        with self.lock:
            self.latest = None
            self.decoded = None
            capture = getattr(self, '_capture', None)
        if isinstance(capture, VaapiCapture):
            capture.release()  # Unblock a read waiting on the FFmpeg pipe.

    def _note_reopen(self, reason, connected, retry_in):
        """Record and log why the stream is about to be reopened (diagnostics only)."""
        stats = self.read_stats()
        with self.lock:
            self.reopens += 1
            self.reopen_reasons[reason] += 1
            self.last_reopen = {'reason': reason, 'connected_s': round(connected, 1),
                                'generation_before': self.generation, 'retry_in_s': retry_in, **stats}
        _LOGGER.info('Stream reader reopening: reason=%s after %.1f s, lag_ms=%s read_wait_ms=%s '
                     'instant_reads=%s%% generation=%d retry_in=%.0f s', reason, connected,
                     stats['lag_ms'], stats['read_wait_ms'], stats['instant_read_pct'],
                     self.generation, retry_in)
        self.reads = self.instant_reads = 0
        self.read_wait_ms = None

    def _note_first_usable(self, elapsed):
        """After a reopen, record how long the new connection took to a usable frame."""
        with self.lock:
            reopen = self.last_reopen
            if reopen is None or 'first_usable_ms' in reopen:
                return
            reopen['first_usable_ms'] = round(elapsed * 1000)
            reopen['generation_after'] = self.generation
        _LOGGER.info('Stream reader reopened: first usable frame after %d ms (generation %d)',
                     round(elapsed * 1000), reopen['generation_after'])

    def run(self):
        delay = 1.0
        while not self.stopped.is_set():
            capture = None
            with self.lock:
                self.state, self.latest = "connecting", None
                self.decoded = None
                self.generation += 1
            started = time.monotonic()
            end_reason = 'open_failed'
            try:
                # Open/read deadlines prevent a dead camera pinning a reader.
                capture = open_capture(self.url, self.decode)
                with self.lock:
                    self._capture = capture
                last_published = 0.0
                started = time.monotonic()
                self.clock_offset = self.last_position = self.position_arrived = None
                self.invalid_clock = False
                self.timestamp_origin = None
                self.lag_ms = self.drift_ms = self.min_offset = None
                self.last_read_live = False
                behind_since = started
                first_usable = None
                end_reason = 'stream_closed' if capture.isOpened() else 'open_failed'
                while capture.isOpened() and not self.stopped.is_set():
                    read_started = time.monotonic()
                    ok, frame = capture.read()
                    self.note_read(time.monotonic() - read_started)
                    if not ok or frame is None:
                        end_reason = 'read_failed'
                        break
                    if frame.nbytes > 32_000_000:
                        end_reason = 'frame_too_large'
                        break
                    if getattr(capture, 'frames', 0) == 1 and isinstance(capture, VaapiCapture):
                        _gpu_started(capture)
                    # Always drain the decoder; never let inference or JPEG work
                    # build a queue of old network frames.
                    now = time.monotonic()
                    position = decoder_position_ms(capture)
                    if not self.accept_position(position, now, started):
                        with self.lock:
                            self.state, self.latest = "catching_up", None
                            self.decoded = None
                        if now - behind_since > 2:
                            end_reason = 'catch_up_timeout'
                            break  # Reconnect if decoding cannot catch up.
                        continue
                    behind_since = now
                    if first_usable is None:
                        first_usable = now
                        self._note_first_usable(now - started)
                    with self.lock:
                        if self.timestamp_origin is None:self.timestamp_origin=position
                        self.sequence += 1
                        self.decoded = {'sequence':self.sequence,'generation':self.generation,
                                        'arrived':now,'pts_ms':position-self.timestamp_origin+1,'image':frame}
                    if not self.encode_jpeg:
                        with self.lock:
                            self.state="streaming"
                            self.received=now
                            self.width,self.height=frame.shape[1],frame.shape[0]
                            self.frames+=1
                        delay=1.0
                        continue
                    if now - last_published < .25:
                        continue
                    ok, jpeg = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
                    if ok and jpeg.nbytes <= 20_000_000:
                        self.publish(jpeg.tobytes(), frame.shape[1], frame.shape[0])
                        last_published = now
                        delay = 1.0
            except (cv2.error, OSError, ValueError):
                end_reason = 'decoder_error'  # Never log private stream URLs.
            finally:
                with self.lock:
                    self._capture = None
                if capture is not None:
                    _gpu_result(capture)
                    capture.release()
                with self.lock:
                    self.state, self.latest = "unavailable", None
                    self.decoded = None
            if self.stopped.is_set():
                break
            self._note_reopen(end_reason, time.monotonic() - started, delay)
            if self.stopped.wait(delay):
                break
            delay = min(delay * 2, 30.0)


class LiveFramePool:
    def __init__(self, max_readers=16, observation_linger=0.0):
        self.max_readers = max_readers
        # An ended observation keeps its reader decoding this long, so the next
        # event on that camera starts on a connected stream (no RTSP/keyframe wait).
        self.observation_linger = observation_linger
        self.lingering = {}
        self.linger_timer = None
        self.lock = threading.RLock()
        self.readers = {}
        self.retiring = []
        self.observation_sources = {}
        self.regular_sources = {}
        self.calibration_sources = {}
        self.calibration_timer = None

    def _unused(self, camera):
        return (camera in self.readers and camera not in self.observation_sources
                and camera not in self.regular_sources and camera not in self.calibration_sources
                and camera not in self.lingering)

    def _retire(self, camera):
        self.lingering.pop(camera, None)
        reader = self.readers.pop(camera)
        reader.stop()
        self.retiring.append(reader)

    def ensure(self, camera, url):
        with self.lock:
            reader = self.readers.get(camera)
            if camera in self.observation_sources and self.observation_sources[camera] != url:
                return None
            if reader is not None and reader.url == url:
                return reader
            if reader is not None:
                self._retire(camera)
            self.retiring = [item for item in self.retiring if item.thread and item.thread.is_alive()]
            if len(self.readers) + len(self.retiring) >= self.max_readers:
                return None
            # A changed per-camera decode applies when the reader is next created.
            reader = LiveFrameReader(url, decode=CAMERA_DECODE.get(camera))
            self.readers[camera] = reader
            reader.start()
            return reader

    def sync(self, sources):
        with self.lock:
            self.regular_sources = dict(sources)
            for camera in list(self.readers):
                if camera not in sources and self._unused(camera):
                    self._retire(camera)
                else:
                    # JPEG demand is symmetric: a surviving observation lease
                    # alone must stop the former snapshot consumer's encoding.
                    self.readers[camera].encode_jpeg = camera in sources
            for camera, url in sources.items():
                reader=self.ensure(camera, url)
                if reader is not None:reader.encode_jpeg=True

    def acquire_observation(self, camera, url):
        with self.lock:
            retiring=sum(bool(getattr(item,'observation',False)) and item.thread is not None and item.thread.is_alive() for item in self.retiring)
            if camera not in self.observation_sources and len(self.observation_sources)+retiring >= 2:
                return None
            if camera in self.regular_sources and self.regular_sources[camera] != url:
                return None
            reader=self.ensure(camera,url)
            if reader is not None:
                self.lingering.pop(camera,None)
                reader.observation=True
                reader.encode_jpeg=camera in self.regular_sources
                self.observation_sources[camera]=url
            return reader

    def release_observation(self, camera):
        with self.lock:
            self.observation_sources.pop(camera,None)
            if (camera in self.readers and self.observation_linger > 0
                    and camera not in self.regular_sources and camera in self.calibration_sources):
                # A preview lease is open: keep the reader warm after it expires too.
                self.lingering[camera]=time.monotonic()+self.observation_linger
                self._schedule_linger()
                return
            if not self._unused(camera):return
            if self.observation_linger <= 0:
                self._retire(camera);return
            # At most one idle warm reader: it costs one decoder thread.
            for other in [c for c in self.lingering if c != camera]:
                self.lingering.pop(other)
                if self._unused(other):self._retire(other)
            self.lingering[camera]=time.monotonic()+self.observation_linger
            self._schedule_linger()

    def _schedule_linger(self):
        if self.linger_timer is not None or not self.lingering:
            return
        delay=max(0.0,min(self.lingering.values())-time.monotonic())+0.05
        self.linger_timer=threading.Timer(delay,self._expire_linger)
        self.linger_timer.daemon=True
        self.linger_timer.start()

    def _expire_linger(self):
        with self.lock:
            self.linger_timer=None
            now=time.monotonic()
            for camera,deadline in list(self.lingering.items()):
                if deadline<=now:
                    self.lingering.pop(camera)
                    if self._unused(camera):self._retire(camera)
            self._schedule_linger()

    def acquire_calibration(self, camera, url):
        """Short renewable preview lease; never replace an active production source."""
        with self.lock:
            for sources in (self.observation_sources, self.regular_sources):
                if camera in sources and sources[camera] != url:
                    return None
            if camera not in self.calibration_sources and len(self.calibration_sources) >= 1:
                return None
            retiring = sum(bool(getattr(item, 'observation', False)) and item.thread is not None
                           and item.thread.is_alive() for item in self.retiring)
            if camera not in self.observation_sources and len(set(self.observation_sources) | set(self.calibration_sources)) + retiring >= 2 and camera not in self.calibration_sources:
                return None
            reader = self.ensure(camera, url)
            if reader is None:
                return None
            reader.observation = True
            reader.encode_jpeg = camera in self.regular_sources
            self.calibration_sources[camera] = time.monotonic() + 5
            if self.calibration_timer is None:
                self._schedule_calibration_expiry()
            return reader

    def _schedule_calibration_expiry(self):
        self.calibration_timer = threading.Timer(5, self._expire_calibration)
        self.calibration_timer.daemon = True
        self.calibration_timer.start()

    def _expire_calibration(self):
        with self.lock:
            self.calibration_timer = None
            for camera, deadline in list(self.calibration_sources.items()):
                if time.monotonic() >= deadline:
                    self.calibration_sources.pop(camera, None)
                    if self._unused(camera):
                        self._retire(camera)
            if self.calibration_sources:
                self._schedule_calibration_expiry()

    def report(self):
        with self.lock:
            return {camera: {**reader.report(),
                             "regular_consumer":camera in self.regular_sources,
                             "observation_consumer":camera in self.observation_sources,
                             "calibration_consumer":camera in self.calibration_sources,
                             "warm_until_s":round(max(0.0,self.lingering[camera]-time.monotonic()),1)
                                            if camera in self.lingering else None}
                    for camera, reader in self.readers.items()}

    def close(self):
        with self.lock:
            if self.calibration_timer is not None:
                self.calibration_timer.cancel()
                self.calibration_timer = None
            if self.linger_timer is not None:
                self.linger_timer.cancel()
                self.linger_timer = None
            self.calibration_sources.clear()
            self.observation_sources.clear()
            self.lingering.clear()
            self.sync({})
