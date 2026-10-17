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
                CREATE TABLE IF NOT EXISTS face_indexed (
                    event_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL);
            ''')
            con.execute('DELETE FROM face_samples WHERE event_id NOT IN (SELECT event_id FROM events)')
            con.execute('DELETE FROM face_indexed WHERE event_id NOT IN (SELECT event_id FROM events)')
        self.prune_images()

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
                'mode':'identity' if person else 'similarity', 'index':dict(self.index_status)}

    def start_index(self):
        with self._index_lock:
            if self.index_status['running']:
                return dict(self.index_status)
            self.index_status = {'running':True,'processed':0,'indexed':0,'total':0,'error':None}
        def run():
            try:
                self.index_existing()
            except Exception:
                log.exception('Historical face indexing failed')
                self.index_status['error'] = 'סריקת ההיסטוריה נכשלה; אפשר לנסות שוב'
            finally:
                with self._index_lock:
                    self.index_status['running'] = False
        threading.Thread(target=run, daemon=True, name='face-search-index').start()
        return dict(self.index_status)

    def index_existing(self):
        # Existing unknowns carry original embeddings; no inference or relabeling needed.
        for path in self.gallery.unknown_dir.glob('*.json'):
            try:
                meta = json.loads(path.read_text(encoding='utf-8'))
                if meta.get('event_id') and meta.get('embedding'):
                    self.record(meta['event_id'], meta['embedding'], cv2.imread(str(path.with_suffix('.jpg'))))
            except (ValueError,OSError,KeyError):
                continue
        with self.audit._connection() as con:
            events = con.execute('SELECT event_id FROM events ORDER BY start_ts DESC').fetchall()
        # Only retained evidence is recoverable. Never download all Frigate history implicitly.
        files = [(eid, self.audit.evidence_path(eid)) for (eid,) in events]
        files = [(eid,path) for eid,path in files if path.is_file()]
        self.index_status['total'] = len(files)
        for eid,path in files:
            fingerprint = f'{path.stat().st_mtime_ns}:{path.stat().st_size}'
            with self.audit._connection() as con:
                old = con.execute('SELECT fingerprint FROM face_indexed WHERE event_id=?',(eid,)).fetchone()
            if not old or old[0] != fingerprint:
                image = cv2.imread(str(path))
                if image is not None:
                    for face in self.engine.faces(image)[:8]:
                        self.record(eid, face.normed_embedding, crop_face(image,face.bbox))
                        self.index_status['indexed'] += 1
                with self.audit._connection() as con:
                    con.execute('INSERT OR REPLACE INTO face_indexed VALUES(?,?)',(eid,fingerprint))
            self.index_status['processed'] += 1


def register_face_routes(app, processor, gallery, engine):
    audit = getattr(processor,'audit',None)
    if audit is None or not hasattr(audit, '_connection'):
        return
    service = getattr(processor,'face_search',None) or FaceSearch(audit,gallery,engine)
    processor.face_search = service
    tokens, token_lock = {}, threading.Lock()

    @app.get('/api/faces/index')
    def index_status():
        return dict(service.index_status)

    @app.get('/api/faces/event/{event_id}')
    def event_faces(event_id: str):
        with audit._connection() as con:
            rows = con.execute('SELECT sample_id FROM face_samples WHERE event_id=? ORDER BY created_ts', (event_id,)).fetchall()
        return {'samples':[{'sample_id':sid,'image':f'api/faces/images/{sid}' if (service.image_dir/f'{sid}.jpg').is_file() else None} for (sid,) in rows]}

    @app.post('/api/faces/index')
    def index_history():
        return service.start_index()

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
        with token_lock:
            now=time.time()
            for key in list(tokens):
                if tokens[key][0]<now: tokens.pop(key)
            while len(tokens)>=20: tokens.pop(next(iter(tokens)))
            token=secrets.token_urlsafe(24); tokens[token]=(now+600,vector,crop.tobytes(),quality.usable)
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
                slug=body.get('person')
                if slug:
                    if slug not in gallery.persons(): raise HTTPException(404,'האדם אינו קיים בגלריה')
                else:
                    name=body.get('name')
                    if not isinstance(name,str) or not name.strip() or len(name)>200:
                        raise HTTPException(400,'יש להזין שם תקין')
                    slug=gallery.create_person(name.strip())
                crop_image=cv2.imdecode(np.frombuffer(saved[2],dtype=np.uint8),cv2.IMREAD_COLOR)
                filename=gallery.add_face(slug,crop_image,saved[1],source={'camera':'upload'})
            tokens.pop(token,None)
        gallery.refresh_guesses()
        return {'added':1,'slug':slug,'file':filename}

    @app.get('/api/faces/timeline')
    def timeline(person: str | None=None, unknown_id: str | None=None, event_id: str | None=None,
                 sample_id: str | None=None, token: str | None=None, threshold:float=.55,
                 camera:str | None=None, date_from:float | None=None, date_to:float | None=None,
                 offset:int=0,limit:int=50,order:str='asc',similar:bool=False):
        if sum(bool(x) for x in (person,unknown_id,event_id,sample_id,token)) != 1:
            raise HTTPException(400,'בחר מקור פנים אחד לחיפוש')
        vectors, name = [], None
        try:
            if person:
                name,vectors=service.person_vectors(person)
            elif unknown_id:
                if not re.fullmatch(r'[A-Za-z0-9_-]+',unknown_id):
                    raise ValueError('Invalid face id')
                path=gallery.unknown_dir/f'{unknown_id}.json'
                if not path.is_file(): raise HTTPException(404,'הפנים כבר טופלו או הוסרו מהתור')
                vectors=[json.loads(path.read_text(encoding='utf-8'))['embedding']]
            elif token:
                with token_lock:
                    saved=tokens.get(token)
                if not saved or saved[0]<time.time():
                    raise HTTPException(410,'החיפוש פג; העלה את התמונה שוב')
                vectors=[saved[1]]
            else:
                vectors=service.sample_vectors(event_id=event_id,sample_id=sample_id)
                if not vectors: raise HTTPException(404,'אין עדיין נתוני פנים לחיפוש באירוע; סרוק את ההיסטוריה הזמינה')
                if event_id and len(vectors)>1:
                    raise HTTPException(409,'באירוע יש כמה תמונות פנים; בחר אחת לחיפוש')
            return service.timeline(vectors,person=name if person and not similar else None,
                threshold=threshold,camera=camera,date_from=date_from,date_to=date_to,offset=offset,limit=limit,order=order)
        except (ValueError,KeyError) as exc:
            raise HTTPException(400,str(exc)) from exc

    @app.get('/api/faces/images/{sample_id}')
    def face_image(sample_id:str):
        if not re.fullmatch(r'[a-f0-9]{64}',sample_id): raise HTTPException(404)
        service.prune_images()
        path=service.image_dir/f'{sample_id}.jpg'
        if not path.is_file(): raise HTTPException(404,'תמונת הפנים אינה זמינה עוד')
        return FileResponse(path,headers={'Cache-Control':'private, no-store'})
