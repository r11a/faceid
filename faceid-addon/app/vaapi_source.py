"""GPU (VAAPI) decoding of a live RTSP stream through the system FFmpeg.

The OpenCV wheel bundles an FFmpeg without VAAPI, so a full-resolution H.265
stream is decoded on the CPU and falls behind on small cores. This source runs
the distribution FFmpeg instead: the GPU decodes and scales, FFmpeg's
``showinfo`` reports each output frame's media time, and frames arrive as BGR
arrays. It mimics the part of ``cv2.VideoCapture`` the reader uses, so the
reader's freshness and backlog rules apply unchanged.
"""
import os
import queue
import re
import subprocess
import threading

import numpy as np

POS_MSEC = 0  # cv2.CAP_PROP_POS_MSEC
OPEN_TIMEOUT = 6.0   # seconds to the first frame (RTSP connect and keyframe)
READ_TIMEOUT = 1.5   # seconds between frames before the stream counts as stalled
_SHOWINFO = re.compile(r'\bn:\s*(\d+)\s+pts:\s*(-?\d+)\s+pts_time:\s*(-?[0-9.]+)')
_SIZE = re.compile(r'\bs:(\d+)x(\d+)\b')


def parse_showinfo(line):
    """Return (pts_ms, width, height) from one showinfo frame line, else None."""
    if 'showinfo' not in line:
        return None
    frame, size = _SHOWINFO.search(line), _SIZE.search(line)
    if not frame or not size:
        return None
    width, height = int(size.group(1)), int(size.group(2))
    if not 0 < width <= 8192 or not 0 < height <= 8192:
        return None
    return float(frame.group(3)) * 1000.0, width, height


def command(url, *, width, step, device, checksum_option=True):
    scale = [f'scale_vaapi=w={int(width)}:h=-2'] if width else []
    showinfo = 'showinfo=checksum=0' if checksum_option else 'showinfo'
    # Thin by frame count after hwdownload: timestamp-based pacing reacts to the
    # restream's connect-time timestamp jumps, and GPU-side filters hold surfaces.
    chain = ','.join([*scale, 'hwdownload', 'format=nv12', f'framestep={int(step)}', showinfo])
    return ['ffmpeg', '-hide_banner', '-nostdin', '-nostats', '-loglevel', 'info',
            '-hwaccel', 'vaapi', '-hwaccel_device', device, '-hwaccel_output_format', 'vaapi',
            '-extra_hw_frames', '8',
            '-rtsp_transport', 'tcp', '-timeout', '5000000', '-i', url,
            # One output frame per showinfo line: the reader takes exactly one frame per
            # report, so FFmpeg must never duplicate or drop frames at the output.
            '-an', '-sn', '-dn', '-vf', chain, '-vsync', 'passthrough',
            '-pix_fmt', 'bgr24', '-f', 'rawvideo', 'pipe:1']


class VaapiCapture:
    """Minimal ``cv2.VideoCapture`` stand-in: isOpened, read, get(POS_MSEC), release."""

    def __init__(self, url, *, width=0, step=3, device='/dev/dri/renderD128',
                 checksum_option=True, popen=subprocess.Popen):
        env = {**os.environ, 'LIBVA_DRIVER_NAME': os.environ.get('LIBVA_DRIVER_NAME', 'iHD')}
        # stderr carries the URL in FFmpeg's banner: it is parsed, never logged.
        self.process = popen(command(url, width=width, step=step, device=device,
                                     checksum_option=checksum_option),
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, env=env, bufsize=0)
        self.checksum_option = checksum_option
        self.decode_width, self.decode_step = width, step
        self.stalled = False
        self.frames = 0
        self.position_ms = float('nan')
        self._open = True
        self._infos = queue.Queue(maxsize=32)
        self._thread = threading.Thread(target=self._parse_stderr, daemon=True,
                                        name='faceid-vaapi-info')
        self._thread.start()

    def _parse_stderr(self):
        try:
            for raw in iter(self.process.stderr.readline, b''):
                info = parse_showinfo(raw.decode('utf-8', 'replace'))
                if info is not None:
                    try:
                        self._infos.put(info, timeout=READ_TIMEOUT)
                    except queue.Full:
                        break
        except (OSError, ValueError):
            pass
        finally:
            try:
                self._infos.put_nowait(None)
            except queue.Full:
                pass

    def isOpened(self):
        return self._open and self.process.poll() is None

    def _read_exact(self, size):
        buffer = bytearray(size)
        view, filled = memoryview(buffer), 0
        while filled < size:
            count = self.process.stdout.readinto(view[filled:])
            if not count:
                return None
            filled += count
        return buffer

    def read(self):
        if not self._open:
            return False, None
        try:
            info = self._infos.get(timeout=READ_TIMEOUT if self.frames else OPEN_TIMEOUT)
        except queue.Empty:
            info = None
            self.stalled = self.frames > 0
        if info is None:
            self._open = False
            return False, None
        position_ms, width, height = info
        try:
            data = self._read_exact(width * height * 3)
        except (OSError, ValueError):
            data = None
        if data is None:
            self._open = False
            return False, None
        self.frames += 1
        self.position_ms = position_ms
        return True, np.frombuffer(data, np.uint8).reshape(height, width, 3)

    def get(self, prop):
        return self.position_ms if prop == POS_MSEC else 0.0

    def release(self):
        self._open = False
        try:
            self.process.kill()
        except OSError:
            pass
        for stream in (self.process.stdout, self.process.stderr):
            try:
                stream.close()
            except (OSError, AttributeError):
                pass
        try:
            self.process.wait(timeout=2)
        except (subprocess.TimeoutExpired, OSError):
            pass
