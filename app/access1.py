"""Permission-preserving Access1 boundary. No vendor transport is assumed.

A verified provider must submit a biometric credential to Access1's authorization
flow, not invoke a remote-unlock command. It returns Access1's own decision/event ID.
"""
import hashlib
import json
import logging
import math
import queue
import sqlite3
import threading
import time
from pathlib import Path

from fastapi import HTTPException

from .gallery import _atomic_write_json

log = logging.getLogger('faceid.access1')


class Access1Integration:
    def __init__(self, data_dir, audit, *, provider=None):
        self.path = Path(data_dir)/'access1.json'
        self.audit, self.provider = audit, provider
        self._lock = threading.RLock()
        self._queue = queue.Queue(maxsize=50)
        self.config = {'enabled':False,'people':{},'cameras':{}}
        if self.path.is_file():
            self.config.update(json.loads(self.path.read_text(encoding='utf-8')))
        with audit._connection() as con:
            con.execute('''CREATE TABLE IF NOT EXISTS access1_attempts (
                request_id TEXT PRIMARY KEY, face_event_id TEXT NOT NULL,
                user_id TEXT NOT NULL, reader_id TEXT NOT NULL,
                ts REAL NOT NULL, state TEXT NOT NULL, decision TEXT,
                access1_event_id TEXT, error TEXT)''')
        if provider is not None:
            threading.Thread(target=self._worker,daemon=True,name='access1-credentials').start()

    def status(self):
        with self._lock:
            config=json.loads(json.dumps(self.config))
        return {**config,'available':self.provider is not None,
                'state':'ready' if self.provider is not None else 'awaiting_api',
                'enabled':bool(config['enabled'] and self.provider is not None),
                'note':'Access1 decides permissions and records its own biometric event. No door command is sent by FaceID.'}

    def configure(self, updates):
        with self._lock:
            if updates.get('enabled') and self.provider is None:
                raise ValueError('נדרש חיבור מאומת ל־API של Access1 לפני הפעלה')
            config=json.loads(json.dumps(self.config))
            for key in ('people','cameras'):
                if key in updates:
                    value=updates[key]
                    if not isinstance(value,dict) or len(value)>1000 or any(
                        not isinstance(k,str) or not k or len(k)>200 or
                        not isinstance(v,str) or not v.strip() or len(v)>200
                        for k,v in value.items()):
                        raise ValueError('מיפוי משתמשים או קוראים אינו תקין')
                    config[key]={k:v.strip() for k,v in value.items()}
            if 'enabled' in updates:
                if type(updates['enabled']) is not bool: raise ValueError('enabled must be boolean')
                config['enabled']=updates['enabled']
            if config['enabled'] and self.provider is not None:
                users={str(u['id']) for u in self.provider.users()}
                readers={str(r['id']) for r in self.provider.readers()}
                if any(v not in users for v in config['people'].values()) or any(v not in readers for v in config['cameras'].values()):
                    raise ValueError('משתמש או קורא אינם קיימים ב־Access1')
            _atomic_write_json(self.path,config,indent=2)
            self.config=config
        return self.status()

    def enqueue(self, event):
        if self.provider is None or not self.config['enabled']:
            return
        try:
            self._queue.put_nowait(dict(event))
        except queue.Full:
            log.error('Access1 queue full; credential was not submitted')

    def _worker(self):
        while True:
            event=self._queue.get()
            try:
                # Delayed credentials must never open a door after the person left.
                if time.time()-event.get('queued_at',time.time()) <= 10:
                    self.present(event)
            except Exception:
                log.exception('Access1 submission failed')
            finally:
                self._queue.task_done()

    def present(self, event):
        with self._lock:
            cfg=json.loads(json.dumps(self.config))
        user_id=cfg['people'].get(event.get('slug'))
        reader_id=cfg['cameras'].get(event.get('camera'))
        evidence_ts=event.get('evidence_ts')
        fresh=(isinstance(evidence_ts,(int,float)) and math.isfinite(evidence_ts)
               and 0 <= time.time()-evidence_ts <= 10
               and event.get('evidence_source') == 'secure_live_hires'
               and not event.get('event_ended'))
        if (not self.provider or not cfg['enabled'] or not user_id or not reader_id
                or not fresh or event.get('liveness_confirmed') is not True or not event.get('event_id')):
            return {'state':'blocked','reason':'disabled_unmapped_or_liveness_unconfirmed'}
        request_id=hashlib.sha256(json.dumps([event['event_id'],reader_id,user_id]).encode()).hexdigest()
        # Claim durably BEFORE the remote call; an ambiguous timeout is never retried
        # as a second door transaction. The vendor must also honor request_id.
        with self.audit._connection() as con:
            try:
                con.execute('INSERT INTO access1_attempts(request_id,face_event_id,user_id,reader_id,ts,state) VALUES(?,?,?,?,?,?)',
                    (request_id,event['event_id'],user_id,reader_id,time.time(),'pending'))
            except sqlite3.IntegrityError:
                return {'state':'duplicate','request_id':request_id}
        try:
            result=self.provider.present_biometric(user_id=user_id,reader_id=reader_id,
                credential_type='face',request_id=request_id,face_event_id=event['event_id'],
                occurred_at=event.get('occurred_at'),score=event.get('score'))
            if result.get('decision') not in ('granted','denied') or not result.get('event_id'):
                raise ValueError('Access1 must return its authorization decision and biometric event ID')
            with self.audit._connection() as con:
                con.execute('UPDATE access1_attempts SET state=?,decision=?,access1_event_id=? WHERE request_id=?',
                    ('confirmed',result['decision'],str(result['event_id']),request_id))
            return {'state':'confirmed','decision':result['decision'],'access1_event_id':str(result['event_id']),'request_id':request_id}
        except Exception:
            with self.audit._connection() as con:
                con.execute('UPDATE access1_attempts SET state=?,error=? WHERE request_id=?',
                    ('unconfirmed','Access1 did not confirm the transaction; inspect Access1 before retrying',request_id))
            log.exception('Access1 did not confirm request %s',request_id)
            return {'state':'unconfirmed','request_id':request_id}

    def recent(self):
        with self.audit._connection() as con:
            con.row_factory=sqlite3.Row
            return [dict(row) for row in con.execute('SELECT * FROM access1_attempts ORDER BY ts DESC LIMIT 100')]


def register_access1_routes(app, processor, gallery, data_dir):
    audit=getattr(processor,'audit',None)
    if audit is None or not hasattr(audit,'_connection'): return
    integration=getattr(processor,'access1',None) or Access1Integration(data_dir,audit)
    processor.access1=integration

    @app.get('/api/access1')
    def access1_status():
        return {**integration.status(),'attempts':integration.recent()}

    @app.get('/api/access1/directory')
    def directory():
        if integration.provider is None:
            return {'users':[],'readers':[],'verified':False}
        return {'users':integration.provider.users(),'readers':integration.provider.readers(),'verified':True}

    @app.post('/api/access1')
    def configure_access1(body:dict):
        if 'people' in body and isinstance(body['people'],dict):
            if any(slug not in gallery.persons() for slug in body['people']):
                raise HTTPException(400,'בחר אדם קיים ב־FaceID')
        try:
            return integration.configure(body)
        except ValueError as exc:
            raise HTTPException(400,str(exc)) from exc
