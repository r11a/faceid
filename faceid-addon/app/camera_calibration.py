"""Source-correct, bounded camera measurements. Never produces access evidence."""
from contextlib import contextmanager
import base64
import threading
import time

import cv2
import numpy as np

from .frigate_api import camera_frame_bytes
from .quality import measure_face_quality


class CalibrationUnavailable(Exception):
    pass


class CameraCalibration:
    def __init__(self, processor, engine):
        self.processor, self.engine = processor, engine
        self.gate = threading.Lock()

    @contextmanager
    def request(self):
        if not self.gate.acquire(blocking=False):
            raise CalibrationUnavailable('calibration_busy')
        try:
            yield
        except CalibrationUnavailable:
            raise
        except TimeoutError:
            raise CalibrationUnavailable('recognition_busy') from None
        except Exception:
            # Transport/decoder exceptions may include an authenticated URL.
            raise CalibrationUnavailable('configured_source_unavailable') from None
        finally:
            self.gate.release()

    def _source(self, camera):
        profiles = self.processor.camera_profiles
        profile = profiles.get(camera)
        source = profile.get('observation_source', 'snapshots')
        if source == 'snapshots':
            return profile, None, None
        profile, direct, revision = profiles.observation_config(camera)
        source = profile.get('observation_source', 'snapshots')
        if source == 'direct_rtsp':
            from .acquisition_diagnostics import validate_direct
            url = validate_direct(direct)
        elif source == 'frigate_restream':
            url = self.processor.frigate._live_url(camera)
        else:
            raise CalibrationUnavailable('invalid_observation_source')
        reader = self.processor.frigate.live_frames.acquire_calibration(camera, url) if url else None
        if reader is None:
            raise CalibrationUnavailable('reader_capacity_or_source_conflict')
        return profile, reader, revision

    def _validate(self, camera, profile, reader, revision, record):
        if reader is not None:
            current, _, current_revision = self.processor.camera_profiles.observation_config(camera)
            if revision != current_revision or reader.generation != record['generation'] or reader.stopped.is_set():
                raise CalibrationUnavailable('source_changed')
        elif self.processor.camera_profiles.get(camera) != profile:
            raise CalibrationUnavailable('source_changed')

    def _snapshot(self, camera, profile):
        content = camera_frame_bytes(self.processor.frigate, camera, profile, timeout=2)
        image = cv2.imdecode(np.frombuffer(content, np.uint8), cv2.IMREAD_COLOR) if content else None
        if image is None:
            raise CalibrationUnavailable('snapshot_unavailable')
        return content, image

    @staticmethod
    def _encode(image):
        ok, data = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, 90])
        if not ok:
            raise CalibrationUnavailable('preview_unavailable')
        return data.tobytes()

    def frame(self, camera):
        with self.request():
            profile, reader, revision = self._source(camera)
            record = None
            if reader is None:
                content, _ = self._snapshot(camera, profile)
            else:
                record = reader.frame_record()
                if record is None:
                    raise CalibrationUnavailable('stream_starting_or_no_valid_frame')
                content = self._encode(record['image'])
            self._validate(camera, profile, reader, revision, record)
            return content

    def analyze(self, camera, include_preview=False):
        with self.request():
            profile, reader, revision = self._source(camera)
            record = None
            if reader is None:
                content, image = self._snapshot(camera, profile)
                with self.engine.background_work(timeout=.25):
                    detected = self.engine.detect_faces(image)
            else:
                # Pick the latest valid frame AFTER admission to the model.
                with self.engine.background_work(timeout=.25):
                    record, detected = self.engine.detect_frame_record(reader)
                if record is None:
                    raise CalibrationUnavailable('stream_starting_or_no_valid_frame')
                image = record['image']
                content = self._encode(image) if include_preview else None
            self._validate(camera, profile, reader, revision, record)
            hour = time.localtime().tm_hour
            minimum = profile.get('night_min_face_px' if hour >= 19 or hour < 6 else 'min_face_px', self.processor.min_face_px)
            faces = []
            for face in detected:
                quality = measure_face_quality(image, face, min_face_px=minimum,
                                               min_quality=self.processor.min_face_quality)
                faces.append({'box':[round(float(value), 1) for value in face.bbox],
                              **quality.to_dict(), 'rejection_reason': None if quality.usable else (
                                  'small_face' if quality.face_px < minimum else
                                  'weak_detection' if quality.detection < .55 else 'low_quality')})
            result = {'camera':camera, 'width':int(image.shape[1]), 'height':int(image.shape[0]),
                      'profile':profile, 'faces':faces, 'min_face_px':minimum,
                      'source':profile.get('observation_source','snapshots'),
                      'purpose':'calibration_only', 'access_evidence':False,
                      'frame_kind':'configured_stream' if reader is not None else 'snapshot_preview'}
            if include_preview:
                result['preview'] = 'data:image/jpeg;base64,' + base64.b64encode(content).decode('ascii')
            return result
