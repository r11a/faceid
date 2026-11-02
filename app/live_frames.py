"""Bounded persistent restream readers; inference consumes only the latest frame."""
import threading
import time
import math
import logging
from collections import Counter
_LOGGER=logging.getLogger(__name__)
import os
# Native decoder errors can contain credential-bearing URLs. Python diagnostics
# remain enabled and report categorical acquisition failures instead.
os.environ['OPENCV_FFMPEG_LOGLEVEL']='-8'
os.environ['OPENCV_LOG_LEVEL']='SILENT'

import cv2
cv2.setLogLevel(0)
from .preview_startup import frame_detail


def decoder_position_ms(capture):
    """FFmpeg presentation timestamp, in its documented FPS time base.

    POS_MSEC is retained for older OpenCV builds without CAP_PROP_PTS. Neither
    property is camera capture wall time. Never substitute arrival time for PTS.
    """
    if hasattr(cv2,'CAP_PROP_PTS'):
        try:
            fps=float(capture.get(cv2.CAP_PROP_FPS))
            pts=float(capture.get(cv2.CAP_PROP_PTS))
            if math.isfinite(fps) and 0<fps<=1000 and math.isfinite(pts):
                return pts*1000/fps
        except (TypeError,ValueError,OverflowError):pass
    return capture.get(cv2.CAP_PROP_POS_MSEC)


class LiveFrameReader:
    def __init__(self, url, *, max_age=1.0):
        self.url = url
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
        self.clock_offset = offset if self.clock_offset is None else min(self.clock_offset, offset)
        valid=now-started>=.5 and offset-self.clock_offset<=.75
        if not valid:self.invalidate_clock("startup_draining" if now-started<.5 else "buffering_growth")
        else:self.invalid_clock=False
        return valid

    def invalidate_clock(self, reason="timestamp_invalid"):
        self.clock_reason=reason
        self.rejections[reason]+=1
        if not self.invalid_clock and reason!="startup_draining" and time.monotonic()-self.last_rejection_log>=5:
            self.last_rejection_log=time.monotonic()
            _LOGGER.info("Stream reader rejected decoded frame: reason=%s",reason)
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
            return dict(self.decoded)

    def report(self):
        with self.lock:
            age = time.monotonic() - self.received if self.received else None
            state = "stopped" if self.stopped.is_set() else self.state
            if state == "streaming" and age is not None and age > self.max_age:
                state = "stale"
            return {"state": state, "width": self.width, "height": self.height,
                    "frames": self.frames, "generation":self.generation, "rejection_reason":self.clock_reason, "rejections":dict(self.rejections), "age_ms": round(age * 1000) if age is not None else None}

    def stop(self):
        self.stopped.set()
        with self.lock:
            self.latest = None
            self.decoded = None

    def run(self):
        delay = 1.0
        while not self.stopped.is_set():
            capture = None
            with self.lock:
                self.state, self.latest = "connecting", None
                self.decoded = None
                self.generation += 1
            try:
                # Open/read deadlines prevent a dead camera pinning a reader.
                # One decoder thread avoids a separate all-core decoder per camera.
                capture = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG, [
                    cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 3000,
                    cv2.CAP_PROP_READ_TIMEOUT_MSEC, 1000,
                    cv2.CAP_PROP_N_THREADS, 1,
                ])
                last_published = 0.0
                started = time.monotonic()
                self.clock_offset = self.last_position = self.position_arrived = None
                self.invalid_clock = False
                self.timestamp_origin = None
                behind_since = started
                while capture.isOpened() and not self.stopped.is_set():
                    ok, frame = capture.read()
                    if not ok or frame is None or frame.nbytes > 32_000_000:
                        break
                    # Always drain the decoder; never let inference or JPEG work
                    # build a queue of old network frames.
                    now = time.monotonic()
                    position = decoder_position_ms(capture)
                    if not self.accept_position(position, now, started):
                        with self.lock:
                            self.state, self.latest = "catching_up", None
                            self.decoded = None
                        if now - behind_since > 2:
                            break  # Reconnect if decoding cannot catch up.
                        continue
                    behind_since = now
                    with self.lock:
                        if self.timestamp_origin is None:self.timestamp_origin=position
                        self.sequence += 1
                        self.decoded = ({'sequence':self.sequence,'generation':self.generation,
                                        'arrived':now,'pts_ms':position-self.timestamp_origin+1,'image':frame}
                                        if frame_detail(frame)['detail']=='structured' else None)
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
                pass  # Never include private stream URLs in logs or diagnostics.
            finally:
                if capture is not None:
                    capture.release()
                with self.lock:
                    self.state, self.latest = "unavailable", None
                    self.decoded = None
            if self.stopped.wait(delay):
                break
            delay = min(delay * 2, 30.0)


class LiveFramePool:
    def __init__(self, max_readers=16):
        self.max_readers = max_readers
        self.lock = threading.RLock()
        self.readers = {}
        self.retiring = []
        self.observation_sources = {}
        self.regular_sources = {}

    def _retire(self, camera):
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
            reader = LiveFrameReader(url)
            self.readers[camera] = reader
            reader.start()
            return reader

    def sync(self, sources):
        with self.lock:
            self.regular_sources = dict(sources)
            for camera in list(self.readers):
                if camera not in sources and camera not in self.observation_sources:
                    self._retire(camera)
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
                reader.observation=True
                reader.encode_jpeg=camera in self.regular_sources
                self.observation_sources[camera]=url
            return reader

    def release_observation(self, camera):
        with self.lock:
            self.observation_sources.pop(camera,None)
            if camera not in self.regular_sources and camera in self.readers:
                self._retire(camera)

    def report(self):
        with self.lock:
            return {camera: reader.report() for camera, reader in self.readers.items()}

    def close(self):
        with self.lock:
            self.observation_sources.clear()
            self.sync({})
