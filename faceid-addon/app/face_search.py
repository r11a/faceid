"""Local face similarity and camera timelines; searches never assign identity."""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import math
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from contextlib import nullcontext

import cv2
import numpy as np
from fastapi import HTTPException, UploadFile
from fastapi.responses import FileResponse

from .engine import crop_face
from .gallery import _atomic_write_bytes
from .quality import measure_face_quality
from starlette.concurrency import run_in_threadpool

log = logging.getLogger('faceid.face_search')


def normalized_vector(value):
    vector = np.asarray(value, dtype=np.float32)
    if vector.shape != (512,) or not np.isfinite(vector).all():
        raise ValueError('Expected a finite 512-dimensional face vector')
    norm = float(np.linalg.norm(vector))
    if norm < 1e-8:
        raise ValueError('Face vector is empty')
    return vector / norm


class FaceSearch:
    def __init__(self, audit, gallery, engine):
        self.audit, self.gallery, self.engine = audit, gallery, engine
        self.frigate = None
        self.image_dir = audit.path.parent / 'face_search_images'
        self.image_dir.mkdir(parents=True, exist_ok=True)
        self._index_lock = threading.Lock()
        self.index_status = {'running': False, 'processed': 0, 'indexed': 0, 'total': 0, 'error': None}
        with audit._lock, audit._connection() as con:
            con.executescript('''
                CREATE TABLE IF NOT EXISTS face_samples (
                    sample_id TEXT PRIMARY KEY, event_id TEXT NOT NULL,
                    embedding BLOB NOT NULL, created_ts REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS face_samples_event_idx ON face_samples(event_id);
                CREATE TABLE IF NOT EXISTS face_scan_attempts (
                    event_id TEXT PRIMARY KEY, attempted_ts REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS face_indexed (
                    event_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL);
            ''')
            con.execute('DELETE FROM face_samples WHERE event_id NOT IN (SELECT event_id FROM events)')
            con.execute('DELETE FROM face_indexed WHERE event_id NOT IN (SELECT event_id FROM events)')
            con.execute('DELETE FROM face_scan_attempts WHERE event_id NOT IN (SELECT event_id FROM events)')
        self.prune_images()

    def coverage(self):
        with self.audit._connection() as con:
            total, indexed = con.execute(
                'SELECT COUNT(*), COALESCE(SUM(EXISTS(SELECT 1 FROM face_samples s '
                'WHERE s.event_id=e.event_id)),0) FROM events e').fetchone()
        return {**self.index_status, 'events_total': total, 'events_indexed': indexed,
                'events_missing': total-indexed, 'coverage_percent': round(100*indexed/total,1) if total else 0,
                'remote_available': self.frigate is not None}

    def record(self, event_id, embedding, crop=None):
        vector = normalized_vector(embedding)
        sample_id = hashlib.sha256(event_id.encode() + vector.tobytes()).hexdigest()
        with self.audit._lock, self.audit._connection() as con:
            if not con.execute('SELECT 1 FROM events WHERE event_id=?', (event_id,)).fetchone():
                return None
            rows = con.execute('SELECT sample_id, embedding FROM face_samples WHERE event_id=?', (event_id,)).fetchall()
            for sid, blob in rows:
                if float(np.frombuffer(blob, dtype=np.float32) @ vector) >= .995:
                    return sid
            if len(rows) >= 8:
                return None
            con.execute('INSERT OR IGNORE INTO face_samples VALUES(?,?,?,?)',
                        (sample_id, event_id, vector.tobytes(), time.time()))
        if crop is not None and crop.size:
            height, width = crop.shape[:2]
            scale = min(1., 200 / max(height, width))
            image = cv2.resize(crop, (max(1, int(width*scale)), max(1, int(height*scale))))
            ok, encoded = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, 85])
            if ok:
                _atomic_write_bytes(self.image_dir / f'{sample_id}.jpg', encoded.tobytes())
                self.prune_images()
        return sample_id

    def prune_images(self):
        self.audit.prune_face_evidence()

    def sample_vectors(self, *, event_id=None, sample_id=None):
        column, value = ('sample_id', sample_id) if sample_id else ('event_id', event_id)
        with self.audit._lock, self.audit._connection() as con:
            rows = con.execute(f'SELECT embedding FROM face_samples WHERE {column}=?', (value,)).fetchall()
        return [np.frombuffer(row[0], dtype=np.float32).copy() for row in rows]

    def person_vectors(self, slug):
        with self.gallery._lock:
            entry = self.gallery._cache.get(slug)
            if entry is None:
                raise ValueError('Unknown person')
            return entry['name'], entry['emb'].copy()

    def gallery_matches(self, vector, threshold=.35):
        results = []
        with self.gallery._lock:
            for slug, entry in self.gallery._cache.items():
                if not len(entry['files']):
                    continue
                sims = entry['emb'] @ vector
                index = int(np.argmax(sims))
                score = float(sims[index])
                if score >= threshold:
                    results.append({'slug': slug, 'name': entry['name'], 'similarity': round(score,4),
                                    'photo': f"media/persons/{slug}/{entry['files'][index]}"})
        return sorted(results, key=lambda row: row['similarity'], reverse=True)[:10]

    def timeline(self, vectors=(), *, person=None, threshold=.55, camera=None,
                 date_from=None, date_to=None, offset=0, limit=50, order='asc'):
        if not math.isfinite(threshold) or not .2 <= threshold <= 1:
            raise ValueError('Similarity threshold must be between 0.2 and 1')
        if order not in ('asc','desc'):
            raise ValueError('Order must be asc or desc')
        clauses, params = [], []
        if person:
            # Explicit review supersedes the original recognition, never a guess.
            clauses.append('(e.ground_truth=? OR (e.ground_truth IS NULL AND e.status=\'recognized\' AND e.person=?))')
            params.extend((person,person))
        if camera:
            clauses.append('e.camera=?'); params.append(camera)
        for value, op in [(date_from, '>='),(date_to, '<=')]:
            if value is not None:
                if not math.isfinite(value):
                    raise ValueError('Invalid date')
                clauses.append(f'COALESCE(e.start_ts,e.updated_ts){op}?'); params.append(value)
        if date_from is not None and date_to is not None and date_from > date_to:
            raise ValueError('Start date must precede end date')
        where = ' WHERE '+' AND '.join(clauses) if clauses else ''
        probes = np.asarray([normalized_vector(v) for v in vectors], dtype=np.float32)
        events = {}
        # Stream rows instead of loading every vector in the database into RAM.
        with self.audit._connection() as con:
            con.row_factory = sqlite3.Row
            rows = con.execute('SELECT e.event_id,e.camera,e.start_ts,e.updated_ts,e.status,e.person,'
                'e.ground_truth,e.score,s.sample_id,s.embedding FROM events e '
                'LEFT JOIN face_samples s ON e.event_id=s.event_id'+where, params)
            for raw in rows:
                row = dict(raw)
                blob = row.pop('embedding')
                similarity = None
                if not person:
                    if blob is None or not len(probes):
                        continue
                    vector = np.frombuffer(blob, dtype=np.float32)
                    similarity = float(np.max(probes @ vector))
                    if similarity < threshold:
                        continue
                sid = row.get('sample_id')
                row['similarity'] = round(similarity,4) if similarity is not None else None
                row['image'] = f'api/faces/images/{sid}' if sid and (self.image_dir/f'{sid}.jpg').is_file() else None
                previous = events.get(row['event_id'])
                if previous is None or (similarity is not None and similarity > (previous['similarity'] or 0)):
                    events[row['event_id']] = row
        ordered = sorted(events.values(), key=lambda r:(r['start_ts'] if r['start_ts'] is not None else r['updated_ts'],r['event_id']), reverse=order=='desc')
        offset, limit = max(0,offset), max(1,min(limit,100))
        return {'events': ordered[offset:offset+limit], 'total':len(ordered),
                'offset':offset, 'limit':limit, 'cameras':sorted({r['camera'] for r in ordered}),
                'mode':'identity' if person else 'similarity', 'index':self.coverage()}

    def start_index(self, *, remote=False, limit=500):
        if not 1 <= limit <= 2000:
            raise ValueError('Scan limit must be between 1 and 2000')
        if remote and self.frigate is None:
            raise ValueError('Frigate is unavailable')
        with self._index_lock:
            if self.index_status['running']:
                return dict(self.index_status)
            self.index_status = {'running':True,'processed':0,'indexed':0,'total':0,'error':None,
                                 'remote':remote,'limit':limit,'unavailable':0,'no_faces':0}
        def run():
            try:
                self.index_existing(remote=remote, limit=limit)
            except Exception:
                log.exception('Historical face indexing failed')
                self.index_status['error'] = 'סריקת ההיסטוריה נכשלה; אפשר לנסות שוב'
            finally:
                with self._index_lock:
                    self.index_status['running'] = False
        threading.Thread(target=run, daemon=True, name='face-search-index').start()
        return self.coverage()

    def _recover_unknowns(self, event_id=None):
        # Reuse original vectors; redetecting a tight stored crop may find no face.
        for path in self.gallery.unknown_dir.glob('*.json'):
            try:
                meta = json.loads(path.read_text(encoding='utf-8'))
                if not isinstance(meta, dict):
                    continue
                eid = meta.get('event_id')
                if isinstance(eid, str) and eid and (event_id is None or eid == event_id) and meta.get('embedding'):
                    self.record(eid, meta['embedding'], cv2.imread(str(path.with_suffix('.jpg'))))
            except (ValueError, TypeError, OSError, KeyError):
                continue

    def _index_evidence(self, eid, path):
        if self.engine is None:
            return 0
        try:
            stat = path.stat()
        except FileNotFoundError:
            return 0  # Evidence may expire while a scan is running.
        fingerprint = f'{stat.st_mtime_ns}:{stat.st_size}'
        with self.audit._connection() as con:
            old = con.execute('SELECT fingerprint FROM face_indexed WHERE event_id=?', (eid,)).fetchone()
            has_samples = con.execute('SELECT 1 FROM face_samples WHERE event_id=? LIMIT 1', (eid,)).fetchone()
        if old and old[0] == fingerprint and has_samples:
            return 0
        image = cv2.imread(str(path))
        if image is None:
            return 0
        indexed = self._index_image(eid, image)
        # A failed/empty detection must remain retryable, including old markers.
        if indexed:
            with self.audit._connection() as con:
                con.execute('INSERT OR REPLACE INTO face_indexed VALUES(?,?)', (eid, fingerprint))
        return indexed

    def _index_image(self, eid, image):
        indexed = 0
        for face in self.engine.faces(image)[:8]:
            if self.record(eid, face.normed_embedding, crop_face(image, face.bbox)):
                indexed += 1
        return indexed

    def recover_event(self, event_id):
        """Recover only the selected event from locally retained evidence."""
        if self.sample_vectors(event_id=event_id):
            return
        with self.audit._connection() as con:
            if not con.execute('SELECT 1 FROM events WHERE event_id=?', (event_id,)).fetchone():
                return
        self._recover_unknowns(event_id)
        if not self.sample_vectors(event_id=event_id):
            self._index_evidence(event_id, self.audit.evidence_path(event_id))

    def index_existing(self, *, remote=False, limit=500):
        if not 1 <= limit <= 2000:
            raise ValueError('Scan limit must be between 1 and 2000')
        self._recover_unknowns()
        if self.engine is None:
            return
        with self.audit._connection() as con:
            # Indexed events need no inference or throttle delay. Explicit remote
            # scans bound the entire missing corpus batch, including local images.
            query = ('SELECT e.event_id FROM events e LEFT JOIN face_scan_attempts a '
                     'ON a.event_id=e.event_id WHERE NOT EXISTS '
                     '(SELECT 1 FROM face_samples s WHERE s.event_id=e.event_id) '
                     'ORDER BY COALESCE(a.attempted_ts,0), COALESCE(e.start_ts,e.updated_ts) DESC')
            events = con.execute(query + (' LIMIT ?' if remote else ''), (limit,) if remote else ()).fetchall()
        candidates = [(eid, self.audit.evidence_path(eid)) for (eid,) in events]
        if not remote:
            candidates = [(eid,path) for eid,path in candidates if path.is_file()]
        self.index_status['total'] = len(candidates)
        background = self.engine.background_work() if hasattr(self.engine, 'background_work') else nullcontext()
        with background:
            for eid,path in candidates:
                if remote:
                    with self.audit._connection() as con:
                        con.execute('INSERT OR REPLACE INTO face_scan_attempts VALUES(?,?)', (eid,time.time()))
                try:
                    indexed = self._index_evidence(eid, path) if path.is_file() else 0
                    if not indexed and remote and self.frigate is not None:
                        image = self.frigate.snapshot(eid, crop=True)
                        if image is None:
                            self.index_status['unavailable'] = self.index_status.get('unavailable',0) + 1
                        else:
                            indexed = self._index_image(eid, image)
                            if not indexed:
                                self.index_status['no_faces'] = self.index_status.get('no_faces',0) + 1
                    elif not indexed:
                        self.index_status['no_faces'] = self.index_status.get('no_faces',0) + 1
                    self.index_status['indexed'] += indexed
                except Exception:
                    log.exception('Historical face snapshot failed for %s', eid)
                    self.index_status['unavailable'] = self.index_status.get('unavailable',0) + 1
                finally:
                    self.index_status['processed'] += 1
                    time.sleep(.1)


def register_face_routes(app, processor, gallery, engine):
    audit = getattr(processor,'audit',None)
    if audit is None or not hasattr(audit, '_connection'):
        return
    service = getattr(processor,'face_search',None) or FaceSearch(audit,gallery,engine)
    processor.face_search = service
    service.frigate = getattr(processor, 'frigate', None)
    tokens, token_lock = {}, threading.Lock()

    def confirmed_event(event_id, person):
        people = gallery.persons()
        if person not in people:
            raise HTTPException(404, 'האדם אינו קיים בגלריה')
        with audit._connection() as con:
            con.row_factory = sqlite3.Row
            row = con.execute('SELECT event_id,camera,start_ts,status,person,ground_truth FROM events WHERE event_id=?', (event_id,)).fetchone()
        if row is None:
            raise HTTPException(404, 'האירוע אינו קיים')
        identity = row['ground_truth'] if row['ground_truth'] is not None else row['person'] if row['status'] == 'recognized' else None
        if identity != people[person]['name']:
            raise HTTPException(409, 'האירוע אינו משויך לאדם שנבחר; אשר תחילה את זהותו')
        return dict(row)

    def remember_face(vector, encoded, usable, context=None):
        with token_lock:
            now = time.time()
            for key in list(tokens):
                if tokens[key][0] < now:
                    tokens.pop(key)
            while len(tokens) >= 20:
                tokens.pop(next(iter(tokens)))
            token = secrets.token_urlsafe(24)
            tokens[token] = (now + 600, vector, encoded, usable, context)
        return token

    @app.get('/api/faces/event/{event_id}/references')
    def history_reference_candidates(event_id: str, person: str):
        """Preview explicit choices from assigned history; opening it never enrolls."""
        event = confirmed_event(event_id, person)
        service.prune_images()
        with audit._connection() as con:
            rows = con.execute('SELECT sample_id,embedding FROM face_samples WHERE event_id=? ORDER BY created_ts,sample_id', (event_id,)).fetchall()
        selected = []
        for sample_id, blob in rows:
            image = cv2.imread(str(service.image_dir / f'{sample_id}.jpg'))
            if image is not None:
                selected.append((sample_id, image, np.frombuffer(blob, dtype=np.float32), True))
        if not selected:
            path = audit.evidence_path(event_id)
            image = cv2.imread(str(path)) if path.is_file() else None
            if image is None and getattr(processor, 'frigate', None) is not None:
                image = processor.frigate.snapshot(event_id, crop=True)
            if image is None:
                raise HTTPException(410, 'תמונת האירוע כבר אינה זמינה; העלה תמונת ייחוס חדשה מעמוד חיפוש פנים')
            if engine is None:
                raise HTTPException(503, 'מנוע הפנים אינו זמין כרגע; נסה שוב או העלה תמונה')
            for face in engine.faces(image)[:8]:
                quality = measure_face_quality(image, face, min_face_px=60,
                    min_quality=max(.32, getattr(processor, 'min_face_quality', .35)))
                selected.append((None, crop_face(image, face.bbox), face.normed_embedding, quality.usable))
        if not selected:
            raise HTTPException(422, 'לא נמצאו פנים לבחירה בתמונה שנשמרה; העלה תמונת ייחוס ברורה יותר')
        candidates = []
        for sample_id, crop, vector, detector_usable in selected:
            if crop is None or not crop.size:
                continue
            candidate, reason = gallery.prepare_reference(crop, vector)
            ok, encoded = cv2.imencode('.jpg', crop, [cv2.IMWRITE_JPEG_QUALITY, 92])
            if not ok:
                continue
            usable = candidate is not None and detector_usable
            if not detector_usable and candidate is not None:
                reason = 'low_quality'
            context = {'person': person, 'event_id': event_id, 'sample_id': sample_id,
                       'source': {'camera': event['camera'], 'event_ts': event['start_ts'],
                                  'event_id': event_id, 'enrollment': 'assigned_history'}}
            token = remember_face(vector, encoded.tobytes(), usable, context)
            candidates.append({'token': token, 'sample_id': sample_id, 'usable': usable,
                'reason': reason, 'preview': 'data:image/jpeg;base64,' + base64.b64encode(encoded).decode()})
        if not candidates:
            raise HTTPException(422, 'לא נמצאו פנים תקינות לבחירה; העלה תמונת ייחוס ברורה יותר')
        return {'event_id': event_id, 'person': person, 'candidates': candidates,
                'expires_in': 600, 'status': 'needs_selection'}

    @app.get('/api/faces/index')
    def index_status():
        return service.coverage()

    @app.get('/api/faces/event/{event_id}')
    def event_faces(event_id: str):
        service.recover_event(event_id)
        with audit._connection() as con:
            rows = con.execute('SELECT sample_id FROM face_samples WHERE event_id=? ORDER BY created_ts', (event_id,)).fetchall()
        return {'samples':[{'sample_id':sid,'image':f'api/faces/images/{sid}' if (service.image_dir/f'{sid}.jpg').is_file() else None} for (sid,) in rows]}

    @app.post('/api/faces/index')
    def index_history(remote: bool=False, limit: int=500):
        try:
            return service.start_index(remote=remote, limit=limit)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post('/api/faces/search')
    async def photo_search(file: UploadFile, face_index: int | None = None):
        raw = await file.read(8*1024*1024+1)
        if len(raw) > 8*1024*1024:
            raise HTTPException(413,'התמונה גדולה מ־8MB')
        # Inspect dimensions before decoding potentially huge compressed images.
        from PIL import Image
        import io
        try:
            with Image.open(io.BytesIO(raw)) as image:
                if image.width*image.height > 20_000_000:
                    raise ValueError('התמונה גדולה מדי; עד 20 מגה־פיקסל')
            image = cv2.imdecode(np.frombuffer(raw,dtype=np.uint8),cv2.IMREAD_COLOR)
            if image is None:
                raise ValueError('קובץ התמונה אינו תקין')
        except (ValueError,OSError,Image.DecompressionBombError) as exc:
            raise HTTPException(400,str(exc)) from exc
        detected = await run_in_threadpool(engine.faces, image)
        faces = [(i,f) for i,f in enumerate(detected) if min(f.bbox[2]-f.bbox[0],f.bbox[3]-f.bbox[1]) >= 32]
        if not faces:
            raise HTTPException(422,'לא נמצאו פנים ברורות; נסה תמונה חדה וקרובה יותר')
        if face_index is None and len(faces)>1:
            h,w = image.shape[:2]
            scale = min(1,1200/max(h,w))
            _, preview = cv2.imencode('.jpg',cv2.resize(image,(int(w*scale),int(h*scale))))
            return {'status':'needs_selection','preview':'data:image/jpeg;base64,'+base64.b64encode(preview).decode(),
                    'candidates':[{'index':i,'bbox':[float(f.bbox[0]/w),float(f.bbox[1]/h),float(f.bbox[2]/w),float(f.bbox[3]/h)]} for i,f in faces]}
        selected = faces[0] if face_index is None else next((pair for pair in faces if pair[0]==face_index),None)
        if selected is None:
            raise HTTPException(400,'בחירת פנים אינה תקינה')
        original_index, face = selected
        vector = normalized_vector(face.normed_embedding)
        quality = measure_face_quality(image,face,min_face_px=60,min_quality=max(.32,getattr(processor,'min_face_quality',.35)))
        selected_crop = crop_face(image,face.bbox)
        h,w = selected_crop.shape[:2]
        scale = min(1.,512/max(h,w))
        selected_crop = cv2.resize(selected_crop,(max(1,int(w*scale)),max(1,int(h*scale))))
        _, crop = cv2.imencode('.jpg',selected_crop,[cv2.IMWRITE_JPEG_QUALITY,90])
        token = remember_face(vector, crop.tobytes(), quality.usable)
        return {'status':'ready','token':token,'face_index':original_index,'preview':'data:image/jpeg;base64,'+base64.b64encode(crop).decode(),
                'matches':service.gallery_matches(vector), 'expires_in':600}

    @app.post('/api/faces/enroll')
    def enroll_selected_face(body:dict):
        token=body.get('token')
        if not isinstance(token,str): raise HTTPException(400,'בחר תמונה לחיפוש תחילה')
        # The selected crop AND vector are bound to the server token. Never reuse
        # a detector index in a later inference of a resized group photograph.
        with token_lock:
            saved=tokens.get(token)
            if not saved or saved[0]<time.time(): raise HTTPException(410,'הבחירה פגה; העלה את התמונה שוב')
            if not saved[3]: raise HTTPException(422,'איכות הפנים אינה מספיקה לגלריה; בחר תמונה חדה וקרובה יותר')
            if bool(body.get('person')) == bool(body.get('name')):
                raise HTTPException(400,'בחר אדם קיים או שם לאדם חדש')
            with gallery.assignment_lock:
                context = saved[4]
                if context:
                    if body.get('person') != context['person']:
                        raise HTTPException(409, 'בחירת הפנים שייכת לאדם אחר')
                    confirmed_event(context['event_id'], context['person'])
                    if context.get('sample_id'):
                        with audit._connection() as con:
                            exists = con.execute('SELECT 1 FROM face_samples WHERE event_id=? AND sample_id=?',
                                (context['event_id'], context['sample_id'])).fetchone()
                        if not exists:
                            raise HTTPException(410, 'תמונת הפנים כבר אינה זמינה; בחר תמונה שוב')
                crop_image=cv2.imdecode(np.frombuffer(saved[2],dtype=np.uint8),cv2.IMREAD_COLOR)
                candidate, reason = gallery.prepare_reference(crop_image, saved[1])
                if candidate is None:
                    raise HTTPException(422, 'איכות הפנים אינה מספיקה לתמונת ייחוס: ' + reason)
                slug=body.get('person')
                if slug:
                    if slug not in gallery.persons(): raise HTTPException(404,'האדם אינו קיים בגלריה')
                else:
                    name=body.get('name')
                    if not isinstance(name,str) or not name.strip() or len(name)>200:
                        raise HTTPException(400,'יש להזין שם תקין')
                    slug=gallery.create_person(name.strip())
                status, filename=gallery.add_reference(slug,candidate,
                    source=context['source'] if context else {'camera':'upload'})
            tokens.pop(token,None)
        gallery.refresh_guesses()
        count = gallery.persons()[slug]['count']
        return {'added':int(status == 'learned'),'duplicates':int(status == 'duplicate'),
                'slug':slug,'file':filename,'reference_count':count,'ready':count > 0}

    @app.get('/api/faces/timeline')
    def timeline(person: str | None=None, unknown_id: str | None=None, event_id: str | None=None,
                 sample_id: str | None=None, token: str | None=None, threshold:float=.55,
                 camera:str | None=None, date_from:float | None=None, date_to:float | None=None,
                 offset:int=0,limit:int=50,order:str='asc',similar:bool=False):
        if sum(bool(x) for x in (person,unknown_id,event_id,sample_id,token)) != 1:
            raise HTTPException(400,'בחר מקור פנים אחד לחיפוש')
        vectors, name = [], None
        source_event_id = event_id
        try:
            if person:
                name,vectors=service.person_vectors(person)
            elif unknown_id:
                if not re.fullmatch(r'[A-Za-z0-9_-]+',unknown_id):
                    raise ValueError('Invalid face id')
                path=gallery.unknown_dir/f'{unknown_id}.json'
                if not path.is_file(): raise HTTPException(404,'הפנים כבר טופלו או הוסרו מהתור')
                meta=json.loads(path.read_text(encoding='utf-8'))
                vectors=[meta['embedding']]
                if isinstance(meta.get('event_id'), str) and meta['event_id']:
                    source_event_id = meta['event_id']
                    service.record(meta['event_id'], vectors[0], cv2.imread(str(path.with_suffix('.jpg'))))
            elif token:
                with token_lock:
                    saved=tokens.get(token)
                if not saved or saved[0]<time.time():
                    raise HTTPException(410,'החיפוש פג; העלה את התמונה שוב')
                vectors=[saved[1]]
            else:
                if event_id:
                    service.recover_event(event_id)
                vectors=service.sample_vectors(event_id=event_id,sample_id=sample_id)
                if not vectors: raise HTTPException(404,'אין עדיין נתוני פנים לחיפוש באירוע; סרוק את ההיסטוריה הזמינה')
                if event_id and len(vectors)>1:
                    raise HTTPException(409,'באירוע יש כמה תמונות פנים; בחר אחת לחיפוש')
            if sample_id:
                with audit._connection() as con:
                    row = con.execute('SELECT event_id FROM face_samples WHERE sample_id=?', (sample_id,)).fetchone()
                source_event_id = row[0] if row else None
            result = service.timeline(vectors,person=name if person and not similar else None,
                threshold=threshold,camera=camera,date_from=date_from,date_to=date_to,offset=offset,limit=limit,order=order)
            result['source_event_id'] = source_event_id
            return result
        except (ValueError,KeyError) as exc:
            raise HTTPException(400,str(exc)) from exc

    @app.get('/api/faces/images/{sample_id}')
    def face_image(sample_id:str):
        if not re.fullmatch(r'[a-f0-9]{64}',sample_id): raise HTTPException(404)
        service.prune_images()
        path=service.image_dir/f'{sample_id}.jpg'
        if not path.is_file(): raise HTTPException(404,'תמונת הפנים אינה זמינה עוד')
        return FileResponse(path,headers={'Cache-Control':'private, no-store'})
