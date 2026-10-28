"""Bounded persistent restream readers; inference consumes only the latest frame."""
import threading
import time
import math

import cv2


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

    def accept_position(self, position_ms, now, started):
        """Reject decoder backlog using stream timestamps, not JPEG receipt age."""
        if not isinstance(position_ms, (int, float)) or not math.isfinite(position_ms) or position_ms <= 0:
            return False
        position = position_ms / 1000
        if self.last_position is not None and position <= self.last_position:
            return False
        self.last_position = position
        offset = now - position
        self.clock_offset = offset if self.clock_offset is None else min(self.clock_offset, offset)
        return now - started >= .5 and offset - self.clock_offset <= .75

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

    def report(self):
        with self.lock:
            age = time.monotonic() - self.received if self.received else None
            state = "stopped" if self.stopped.is_set() else self.state
            if state == "streaming" and age is not None and age > self.max_age:
                state = "stale"
            return {"state": state, "width": self.width, "height": self.height,
                    "frames": self.frames, "age_ms": round(age * 1000) if age is not None else None}

    def stop(self):
        self.stopped.set()
        with self.lock:
            self.latest = None

    def run(self):
        delay = 1.0
        while not self.stopped.is_set():
            capture = None
            with self.lock:
                self.state, self.latest = "connecting", None
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
                self.clock_offset = self.last_position = None
                behind_since = started
                while capture.isOpened() and not self.stopped.is_set():
                    ok, frame = capture.read()
                    if not ok or frame is None:
                        break
                    # Always drain the decoder; never let inference or JPEG work
                    # build a queue of old network frames.
                    now = time.monotonic()
                    if not self.accept_position(capture.get(cv2.CAP_PROP_POS_MSEC), now, started):
                        with self.lock:
                            self.state, self.latest = "catching_up", None
                        if now - behind_since > 2:
                            break  # Reconnect if decoding cannot catch up.
                        continue
                    behind_since = now
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
            if self.stopped.wait(delay):
                break
            delay = min(delay * 2, 30.0)


class LiveFramePool:
    def __init__(self, max_readers=16):
        self.max_readers = max_readers
        self.lock = threading.RLock()
        self.readers = {}
        self.retiring = []

    def _retire(self, camera):
        reader = self.readers.pop(camera)
        reader.stop()
        self.retiring.append(reader)

    def ensure(self, camera, url):
        with self.lock:
            reader = self.readers.get(camera)
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
            for camera in list(self.readers):
                if camera not in sources:
                    self._retire(camera)
            for camera, url in sources.items():
                self.ensure(camera, url)

    def report(self):
        with self.lock:
            return {camera: reader.report() for camera, reader in self.readers.items()}

    def close(self):
        self.sync({})
