"""Local browser QA using real FaceID routes/stores and synthetic camera evidence.

Run: python -m uvicorn tests.operational_qa_app:app --host 127.0.0.1 --port 8766
Never imported by the production entry point. No real camera or door is contacted.
"""
import io
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import requests
from fastapi import Body

from app.audit import AuditStore
from app.camera_profiles import CameraProfiles
from app.gallery import Gallery
from app.mqtt_listener import EventProcessor
from app.webui import build_app

ROOT = Path(__file__).parents[1]
TEMP = tempfile.TemporaryDirectory(prefix='faceid-browser-qa-')
DATA = Path(TEMP.name)


def image(seed=1):
    frame = np.random.default_rng(seed).integers(85, 155, (160, 160, 3), dtype=np.uint8)
    cv2.circle(frame, (80, 68), 43, (175, 185, 200), -1)
    cv2.circle(frame, (65, 61), 4, (30, 30, 30), -1)
    cv2.circle(frame, (95, 61), 4, (30, 30, 30), -1)
    cv2.ellipse(frame, (80, 82), (15, 8), 0, 0, 180, (50, 50, 50), 2)
    cv2.rectangle(frame, (30, 125), (130, 159), (70, 110, 100), -1)
    return frame


def vector(axis):
    result = np.zeros(512, dtype=np.float32)
    result[axis] = 1
    return result


class Frigate:
    secure_mode = True
    username = 'local-qa'
    verify_tls = True
    timeout = 1
    base = 'http://local-qa'

    def cameras(self):
        return ['כניסה', 'משרד']

    def snapshot(self, *args, **kwargs):
        return image()

    def latest_frame_bytes(self, camera):
        return cv2.imencode('.jpg', image())[1].tobytes()

    def connection_status(self):
        return {'connected': True, 'reachable': True, 'url': 'Local synthetic QA'}

    def event(self, event_id):
        return {'camera': 'כניסה', 'start_time': time.time()-20, 'end_time': time.time()-10}

    def request(self, method, path, **kwargs):
        result = requests.Response()
        result.status_code = 200
        content = (ROOT / '.venv/home-qa.mp4').read_bytes() if path.endswith('clip.mp4') else b'{}'
        result.headers.update({'Content-Type': 'video/mp4', 'Accept-Ranges': 'bytes'})
        byte_range = kwargs.get('headers', {}).get('Range')
        if byte_range and byte_range.startswith('bytes='):
            start, _, end = byte_range[6:].partition('-')
            start, end = int(start or 0), int(end or len(content)-1)
            total = len(content)
            content = content[start:end+1]
            result.status_code = 206
            result.headers['Content-Range'] = f'bytes {start}-{end}/{total}'
        result.headers['Content-Length'] = str(len(content))
        result.raw = io.BytesIO(content)
        return result


gallery = Gallery(DATA)
audit = AuditStore(DATA / 'audit.db')
engine = SimpleNamespace(faces=lambda frame: [SimpleNamespace(
    bbox=np.array([0, 0, 160, 160]), normed_embedding=vector(1), det_score=.99)],
    health=lambda: {'providers': ['CPUExecutionProvider'], 'model_pack': 'buffalo_l'})
cfg = {'faceid': {}, 'frigate': {'url': 'http://local-qa'}}
processor = EventProcessor(cfg, engine, gallery, Frigate(), audit)
processor.camera_profiles = CameraProfiles(DATA / 'camera_profiles.json')
app = build_app(cfg, engine, gallery, processor, DATA, ROOT / 'static')
known = gallery.create_person('אדם לדוגמה')
gallery.add_face(known, image(), vector(0))
gallery.set_alert_policy(known, watchlist='black', notify_on_recognition=True)
untrained = gallery.create_person('משתמש ללא ייחוס')
for i, (name, axis) in enumerate([('אדם לדוגמה', 0), ('משתמש ללא ייחוס', 1)]):
    eid = f'assigned-{i}'
    audit.start_event(eid, 'כניסה', time.time()-30+i*10)
    audit.finalize(eid, 'unknown' if i else 'recognized', person=None if i else name, score=.82)
    if i:
        audit.set_ground_truth(eid, name, 'QA')
    audit.save_evidence(eid, image(i+1))
    processor.face_search.record(eid, vector(axis), image(i+1))
audit.start_event('unknown-qa', 'משרד', time.time()-10)
audit.finalize('unknown-qa', 'unknown', score=.3)
gallery.save_unknown(image(3), vector(3), {'camera': 'משרד', 'event_id': 'unknown-qa', 'ts': time.time()})


@app.post('/api/qa/recognize')
def recognize(body: dict = Body(...)):
    eid = f'live-{time.time_ns()}'
    camera = body.get('camera', 'כניסה')
    person = body.get('person', 'אדם לדוגמה')
    audit.start_event(eid, camera, time.time())
    audit.finalize(eid, 'recognized', person=person, score=.91)
    audit.save_evidence(eid, image())
    state = processor._new_event_state(eid, camera)
    processor._publish_presence = lambda *args, **kwargs: None
    processor._publish_recognition(eid, state, person, .91, decision_status='recognized')
    return {'event_id': eid}
