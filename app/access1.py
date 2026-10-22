"""Optional Access1 module: identity catalogue and submission, no access policy."""
import ipaddress
import json
import logging
import math
import os
import queue
import secrets
import socket
import sqlite3
import ssl
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4, uuid5
from .access1_transport import Transport

log=logging.getLogger('faceid.access1')


def atomic(path,value):
    temporary=path.with_name(path.name+'.'+uuid4().hex+'.tmp')
    try:
        with temporary.open('x',encoding='utf-8') as handle:
            os.chmod(temporary,0o600);json.dump(value,handle,ensure_ascii=False)
            handle.flush();os.fsync(handle.fileno())
        os.replace(temporary,path)
    finally:temporary.unlink(missing_ok=True)


class Access1Integration:
    def __init__(self,data_dir,audit,*,gallery=None,processor=None,options=None):
        self.path=Path(data_dir)/'access1.json'
        self.audit,self.gallery,self.processor=audit,gallery,processor
        self.options=options or {};self.transport=None;self.server=None
        connector=Path(data_dir)/'access1-connector.json'
        if connector.is_file():
            # Wizard-managed configuration is authoritative after pairing.
            self.options=json.loads(connector.read_text(encoding='utf-8'))
        self._lock=threading.RLock();self._queue=queue.Queue(maxsize=50);self.state='disabled'
        saved=json.loads(self.path.read_text(encoding='utf-8')) if self.path.is_file() else {}
        self.config={'instance_id':str(UUID(saved['instance_id'])) if saved.get('instance_id') else str(uuid4()),
                     'cameras':saved.get('cameras',{}) if saved.get('instance_id') else {}}
        atomic(self.path,self.config)
        with audit._connection() as con:
            con.execute('BEGIN IMMEDIATE')
            columns={row[1] for row in con.execute('PRAGMA table_info(access1_attempts)')}
            if columns and 'payload' not in columns:
                con.row_factory=sqlite3.Row
                history=[dict(row) for row in con.execute('SELECT * FROM access1_attempts')]
                con.execute('DROP TABLE access1_attempts')
            else:history=[]
            con.execute('''CREATE TABLE IF NOT EXISTS access1_attempts (
                request_id TEXT PRIMARY KEY,face_event_id TEXT NOT NULL,ts REAL NOT NULL,
                state TEXT NOT NULL,payload TEXT,result TEXT)''')
            for row in history:
                con.execute('INSERT INTO access1_attempts VALUES(?,?,?,?,?,?)',
                    (row['request_id'],row['face_event_id'],row['ts'],'historical',None,json.dumps(row)))
            con.execute("UPDATE access1_attempts SET state='unconfirmed' WHERE state='pending'")

    def start(self):
        if self.options.get('enabled') is not True:return
        try:
            self.start_directory()
            if self.options.get('access1_url') and self.options.get('access1_token'):
                self.transport=Transport(self.options['access1_url'],self.options['access1_token'],self.options.get('access1_ca'),paired=self.options.get('paired') is True)
                self.start_worker();self.state='ready'
            else:self.state='directory_only'
        except Exception:
            self.transport=None;self.state='configuration_required'
            log.error('Access1 connector unavailable; verify internal interface, HTTPS certificates and credentials')

    def start_worker(self):
        if not getattr(self,'_worker_started',False):
            threading.Thread(target=self._worker,daemon=True,name='access1-identities').start()
            self._worker_started=True

    def status(self):
        return {'instance_id':self.config['instance_id'],'state':self.state,
                'enabled':self.transport is not None,'directory_running':bool(self.server and self.server.started),
                'camera_evidence_ready':self.camera_evidence_ready(),
                'note':'Assignments and access decisions belong to Access1. Configure this module in addon options.'}

    def camera_evidence_ready(self):
        """A source label alone cannot prove camera transport authentication."""
        frigate = getattr(self.processor, 'frigate', None)
        verify = getattr(getattr(frigate, 'session', None), 'verify', False)
        return bool(
            frigate is not None
            and str(getattr(frigate, 'base', '')).startswith('https://')
            and getattr(frigate, 'secure_mode', False) is True
            and getattr(frigate, 'verify_tls', False) is True
            and (verify is True or isinstance(verify, str) and bool(verify))
            and getattr(frigate, 'username', '')
            and getattr(frigate, 'password', '')
            and getattr(frigate, '_authenticated', False) is True
        )

    def person_id(self,slug):
        with self.gallery.assignment_lock:
            if slug not in self.gallery.persons():raise ValueError('Person unavailable')
            root=self.gallery.persons_dir.resolve();folder=(root/slug).resolve()
            if folder.parent!=root or not folder.is_dir():raise ValueError('Person unavailable')
            path=folder/'access1-identity.json'
            if not path.exists():atomic(path,{'id':str(uuid4())})
            return str(UUID(json.loads(path.read_text(encoding='utf-8'))['id']))

    def directory(self):
        configured=bool(self.processor.frigate.base.strip())
        cameras={}
        if configured:
            response=self.processor.frigate.request('GET','/api/config',timeout=3)
            try:
                if response.status_code!=200:raise ValueError('Camera catalogue unavailable')
                cameras=response.json().get('cameras')
                if not isinstance(cameras,dict):raise ValueError('Camera catalogue unavailable')
            finally:response.close()
        names=sorted(name for name in cameras if self.processor.camera_enabled(name))
        with self._lock,self.gallery.assignment_lock:
            old=self.config['cameras'];updated={name:old.get(name) or str(uuid4()) for name in cameras}
            # An unconfigured source is not evidence that saved cameras were deleted.
            if configured and updated!=old:self.config['cameras']=updated;atomic(self.path,self.config)
            people=[{'id':self.person_id(slug),'name':str(person['name'])[:200]}
                    for slug,person in self.gallery.persons().items()]
            return {'schema_version':'1.0','instance_id':self.config['instance_id'],
                    'people':people,'cameras':[{'id':updated[name],'name':name[:200]} for name in names]}

    def directory_app(self):
        from fastapi import FastAPI, Header, HTTPException, Response, Depends
        app=FastAPI(docs_url=None,redoc_url=None,openapi_url=None)
        token=self.options.get('directory_token','')
        if not 32<=len(token)<=256:raise ValueError('Separate read-only token required')
        def directory_auth(response:Response,authorization:str=Header(default='')):
            response.headers['Cache-Control']='no-store'
            if not secrets.compare_digest(authorization.encode(),('Bearer '+token).encode()):raise HTTPException(401,'Authentication required')
        @app.get('/access1/v1/connection',dependencies=[Depends(directory_auth)])
        def connection_check():
            if self.transport is None:raise HTTPException(409,'Configure Access1 URL, service credential and CA; restart FaceID')
            try:return self.transport.check_connection(self.config['instance_id'])
            except Exception:raise HTTPException(502,'Access1 connection verification failed') from None
        @app.get('/access1/v1/directory',dependencies=[Depends(directory_auth)])
        def catalogue():
            try:
                data=self.directory()
                if len(json.dumps(data).encode())>262144:raise ValueError('size')
                return data
            except Exception:raise HTTPException(503,'Directory unavailable') from None
        return app

    def start_directory(self):
        import uvicorn
        try:host=ipaddress.ip_address(socket.gethostbyname(self.options.get('bind_address','')))
        except (ValueError,OSError):raise ValueError('Configure the private addon interface') from None
        # Explicit trusted internal interface only; never wildcard/public/link-local.
        if not any(host in ipaddress.ip_network(net) for net in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16')):
            raise ValueError('Configure the private addon interface')
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.options['tls_cert'],self.options['tls_key'])
        self.server=uvicorn.Server(uvicorn.Config(self.directory_app(),host=str(host),port=8601,
            ssl_certfile=self.options['tls_cert'],ssl_keyfile=self.options['tls_key'],
            access_log=False,log_level='error',limit_concurrency=8,timeout_keep_alive=2))
        self._directory_thread=threading.Thread(target=self.server.run,daemon=True,name='access1-directory')
        self._directory_thread.start()

    def enqueue(self,event):
        if self.transport is None:return
        try:
            # Freeze identity at recognition time; a reused name must not retarget queued work.
            event=dict(event)
            with self._lock:
                event['person_id']=self.person_id(event.get('slug'))
                event['camera_id']=self.config['cameras'].get(event.get('camera'))
            self._queue.put_nowait(event)
        except queue.Full:log.warning('Access1 identity queue full; event not submitted')
        except Exception:log.warning('Access1 identity unavailable; event not submitted')

    def _worker(self):
        while True:
            event=self._queue.get()
            try:self.present(event)
            except Exception:log.warning('Access1 identity processing unavailable; no automatic replay')
            finally:self._queue.task_done()

    def present(self,event):
        if not self.camera_evidence_ready():
            return {'state':'blocked','reason':'camera_transport_untrusted'}
        evidence_ts=event.get('evidence_ts')
        fresh=(isinstance(evidence_ts,(int,float)) and math.isfinite(evidence_ts)
               and 0<=time.time()-evidence_ts<10 and event.get('evidence_source')=='secure_live_hires'
               and not event.get('event_ended'))
        if not self.transport or not fresh or event.get('liveness_confirmed') is not True or not event.get('event_id'):
            return {'state':'blocked','reason':'disabled_stale_or_liveness_unconfirmed'}
        with self._lock:
            camera_id=self.config['cameras'].get(event.get('camera'))
            if not self.processor.camera_enabled(event.get('camera')):
                return {'state':'blocked','reason':'camera_disabled'}
            if not camera_id or camera_id!=event.get('camera_id'):return {'state':'blocked','reason':'refresh_camera_directory'}
            person_id=self.person_id(event.get('slug'))
            if person_id!=event.get('person_id'):return {'state':'blocked','reason':'identity_changed'}
            request_id=str(uuid5(UUID(self.config['instance_id']),json.dumps([event['event_id'],person_id,camera_id])))
        stamp=lambda value:datetime.fromtimestamp(value,timezone.utc).isoformat()
        payload={'schema_version':'1.0','instance_id':self.config['instance_id'],'event_id':request_id,
            'person_id':person_id,'camera_id':camera_id,'recognized_at':stamp(evidence_ts),'expires_at':stamp(evidence_ts+10),
            'recognition':{'score':event.get('score'),'metric':'face_embedding_similarity'},
            'liveness':{'status':'passed','method':'rgb_passive','score':event.get('liveness_score'),
                        'confirmed_frames':event.get('liveness_frames',0)}}
        with self.audit._connection() as con:
            try:con.execute('INSERT INTO access1_attempts VALUES(?,?,?,?,?,?)',
                (request_id,event['event_id'],time.time(),'pending',json.dumps(payload),None))
            except sqlite3.IntegrityError:return {'state':'duplicate','request_id':request_id}
        result=None
        try:result=self.transport.call('POST',request_id,payload)
        except Exception:pass
        deadline=min(evidence_ts+10,time.time()+8)
        while time.time()<deadline and (result is None or result.get('status') in ('pending','dispatched')):
            time.sleep(.3)
            try:result=self.transport.call('GET',request_id)
            except Exception:break
        state=result.get('status') if result else 'unconfirmed'
        if state in ('pending','dispatched'):state='unconfirmed'
        with self.audit._connection() as con:
            con.execute('UPDATE access1_attempts SET state=?,result=? WHERE request_id=?',(state,json.dumps(result),request_id))
        return {'state':state,'request_id':request_id}

    def recent(self):
        with self.audit._connection() as con:
            con.row_factory=sqlite3.Row
            return [dict(row) for row in con.execute('SELECT request_id,face_event_id,ts,state,result FROM access1_attempts ORDER BY ts DESC LIMIT 100')]


def register_access1_routes(app,processor,gallery,data_dir):
    from fastapi import Depends, Response
    from .access1_admin import require_pairing_admin
    integration=getattr(processor,'access1',None)
    if integration is None:return
    from pydantic import BaseModel, ConfigDict, Field, SecretStr
    class PairCode(BaseModel):
        model_config=ConfigDict(extra='forbid')
        code:SecretStr=Field(min_length=100,max_length=16000)
    @app.post('/api/access1/pairing/submit',dependencies=[Depends(require_pairing_admin)])
    def pairing_submit(body:PairCode,response:Response):
        from .access1_pairing import submit
        response.headers['Cache-Control']='no-store'
        return submit(integration,body.code.get_secret_value())
    @app.post('/api/access1/pairing/finish',dependencies=[Depends(require_pairing_admin)])
    def pairing_finish(response:Response):
        from .access1_pairing import finish
        response.headers['Cache-Control']='no-store'
        return finish(integration)
    @app.post('/api/access1/pairing/disconnect',dependencies=[Depends(require_pairing_admin)])
    def pairing_disconnect(response:Response):
        from .access1_pairing import disconnect
        response.headers['Cache-Control']='no-store'
        return disconnect(integration)
    @app.get('/api/access1/pairing/admin')
    def pairing_admin(response:Response,user_id=Depends(require_pairing_admin)):
        response.headers['Cache-Control']='no-store'
        return {'administrator':True}
    @app.get('/api/access1')
    def access1_status():return {**integration.status(),'attempts':integration.recent()}
