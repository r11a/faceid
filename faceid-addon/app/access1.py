"""Optional Access1 module: identity catalogue and submission, no access policy."""
import re
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
        self.security_path=Path(data_dir)/'access1-security.json'
        self.secure=True
        self.last_submission=None
        self._result_queue=queue.Queue(maxsize=50)
        self._security_changed=threading.Event()
        self._connection_check_lock=threading.Lock()
        self._connection_check_cache=None
        try:
            security=json.loads(self.security_path.read_text(encoding='utf-8'))
            # Only a complete, explicitly acknowledged saved decision may relax trust.
            self.secure=not (isinstance(security,dict) and security.get('secure') is False
                            and security.get('risk_acknowledged') is True
                            and isinstance(security.get('approved_by'),str) and bool(security['approved_by']))
        except (OSError,ValueError):pass
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

    def _report_security_once(self):
        with self._lock:
            transport=self.transport;secure=self.secure
        if transport is None:return False
        try:return transport.report_security(self.config['instance_id'],secure)
        except Exception:return False  # Older/offline Access1: indicator stays unknown, access logic unchanged.

    def _security_worker(self):
        while True:
            self._security_changed.clear()
            self._report_security_once()
            self._security_changed.wait(10)

    def connection_status(self):
        # Read-only diagnostic. Never finish pairing, send an identity or enable access.
        with self._connection_check_lock:
            transport=self.transport
            if transport is None:return {'state':'not_connected','requests_enabled':None}
            now=time.monotonic()
            cached=self._connection_check_cache
            if cached and cached[0] is transport and now-cached[1]<3:return dict(cached[2])
            try:
                result=transport.check_connection(self.config['instance_id'])
                value={'state':'connected','requests_enabled':result['requests_enabled']}
            except Exception:value={'state':'unavailable','requests_enabled':None}
            if self.transport is not transport:return {'state':'not_connected','requests_enabled':None}
            self._connection_check_cache=(transport,time.monotonic(),value)
            return dict(value)

    def start_worker(self):
        if not getattr(self,'_security_worker_started',False):
            self._security_worker_started=True
            threading.Thread(target=self._security_worker,daemon=True,name='access1-secure-status').start()
        self._security_changed.set()
        if not getattr(self,'_result_worker_started',False):
            self._result_worker_started=True
            threading.Thread(target=self._result_worker,daemon=True,name='access1-results').start()
        if not getattr(self,'_worker_started',False):
            threading.Thread(target=self._worker,daemon=True,name='access1-identities').start()
            self._worker_started=True

    def status(self):
        profiles=getattr(self.processor,'camera_profiles',None)
        values=profiles.all() if profiles else {}
        stream_cameras=[{'camera':name,'source':p.get('observation_source'),'trusted':p.get('trust_local_video_network') is True}
                        for name,p in (values.items() if isinstance(values,dict) else ())
                        if p.get('observation_source','snapshots')!='snapshots']
        return {'instance_id':self.config['instance_id'],'state':self.state,
                'enabled':self.transport is not None,'directory_running':bool(self.server and self.server.started),
                'camera_evidence_ready':self.camera_evidence_ready(),
                'local_stream_cameras':stream_cameras,
                'secure':self.secure,
                'camera_submission_ready':self.camera_evidence_ready(strict=self.secure),
                'last_submission':self.last_submission,
                'note':'Assignments and access decisions belong to Access1. Configure Secure mode in the Access1 connector settings.'}

    def set_security(self,secure,risk_acknowledged,approved_by):
        if type(secure) is not bool or (secure is False and risk_acknowledged is not True):
            raise ValueError('Explicit risk approval is required to turn Secure off')
        with self._lock:
            atomic(self.security_path,{'secure':secure,'risk_acknowledged':not secure,
                                      'approved_by':approved_by,'updated_at':time.time()})
            self.secure=secure
        self._security_changed.set()
        log.warning('Access1 connector Secure mode changed: %s', 'ON' if secure else 'OFF')
        return {'secure':self.secure}

    def camera_evidence_ready(self,*,strict=True):
        """A source label alone cannot prove camera transport authentication."""
        frigate = getattr(self.processor, 'frigate', None)
        verify = getattr(getattr(frigate, 'session', None), 'verify', False)
        return bool(
            frigate is not None
            and str(getattr(frigate, 'base', '')).startswith('https://')
            and getattr(frigate, 'secure_mode', False) is True
            and (not strict or (getattr(frigate, 'verify_tls', False) is True
                 and (verify is True or isinstance(verify, str) and bool(verify))))
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
        if self.transport is None:
            self.last_submission={'state':'blocked','reason':'connector_disabled','ts':time.time()}
            return
        try:
            # Freeze identity at recognition time; a reused name must not retarget queued work.
            event=dict(event)
            event['_queued_monotonic']=time.perf_counter()
            with self._lock:
                event['person_id']=self.person_id(event.get('slug'))
                event['camera_id']=self.config['cameras'].get(event.get('camera'))
            self._queue.put_nowait(event)
        except queue.Full:log.warning('Access1 identity queue full; event not submitted')
        except Exception:log.warning('Access1 identity unavailable; event not submitted')

    def _worker(self):
        while True:
            event=self._queue.get()
            try:
                self._timing(event,'submission_queue',event.get('_queued_monotonic',time.perf_counter()))
                self.present(event,defer=True)
            except Exception:log.warning('Access1 identity processing unavailable; no automatic replay')
            finally:self._queue.task_done()

    def present(self,event,*,defer=False):
        event=dict(event)
        with self._lock:
            self._submission_sequence=getattr(self,'_submission_sequence',0)+1
            event['_submission_order']=self._submission_sequence
        try:
            result=self._present(event,defer=defer)
        except Exception:
            with self._lock:
                if event['_submission_order']>=getattr(self,'_last_submission_order',0):
                    self._last_submission_order=event['_submission_order']
                    self.last_submission={'state':'blocked','reason':'processing_failed','ts':time.time()}
            raise
        return self._record_submission(event,result)

    def _record_submission(self,event,result):
        manager=getattr(self.processor,'stream_observations',None)
        if manager is not None and event.get('evidence_source')=='bounded_local_stream':
            manager.receipt(event,result)
        with self._lock:
            # A quick result may finish before the submitter records pending.
            # Never replace a terminal receipt with that late pending display.
            if result.get('state') == 'pending':
                with self.audit._connection() as con:
                    row=con.execute('SELECT state FROM access1_attempts WHERE request_id=?',
                                    (result.get('request_id'),)).fetchone()
                if row and row[0] != 'pending':return result
            evidence_ts=event.get('evidence_ts')
            evidence_age=(round(time.time()-evidence_ts,3)
                          if isinstance(evidence_ts,(int,float)) and math.isfinite(evidence_ts) else None)
            if event.get('_submission_order',0)>=getattr(self,'_last_submission_order',0):
                self._last_submission_order=event.get('_submission_order',0)
                self.last_submission={**result,'ts':time.time(),'face_event_id':event.get('event_id'),
                                  'evidence_source':event.get('evidence_source'),'evidence_age_seconds':None if event.get('evidence_source')=='bounded_local_stream' else evidence_age,
                                  'evidence_trust':'local_network' if event.get('evidence_source')=='bounded_local_stream' else 'snapshot',
                                  'event_ended':bool(event.get('event_ended')),
                                  'liveness_confirmed':event.get('liveness_confirmed') is True}
            log.info('Access1 submission event=%s state=%s reason=%s source=%s %s=%s ended=%s live=%s',
                     event.get('event_id'),result.get('state'),result.get('reason',''),
                     event.get('evidence_source'),'recognition_age_seconds' if event.get('evidence_source')=='bounded_local_stream' else 'age_seconds',evidence_age,bool(event.get('event_ended')),
                     event.get('liveness_confirmed') is True)
            return result

    def _present(self,event,*,defer=False):
        stream = event.get('evidence_source') == 'bounded_local_stream'
        if stream:
            manager=getattr(self.processor,'stream_observations',None)
            remaining,reason=manager.evidence(event) if manager else (None,'stream_observer_unavailable')
            if remaining is None:return {'state':'blocked','reason':reason}
            evidence_ts=event.get('evidence_ts')
            if (not self.transport or not event.get('event_id') or event.get('liveness_confirmed') is not True
                    or type(evidence_ts) not in (int,float) or not math.isfinite(evidence_ts)):
                return {'state':'blocked','reason':'stream_evidence_invalid'}
            expires_at=min(time.time()+remaining,evidence_ts+10)
            if expires_at<=max(time.time(),evidence_ts):return {'state':'blocked','reason':'evidence_expired'}
            manager.submitting(event)
        else:
            profiles=getattr(self.processor,'camera_profiles',None)
            profile=profiles.get(event.get('camera')) if profiles else {}
            if isinstance(profile,dict) and profile.get('observation_source','snapshots')!='snapshots':
                return {'state':'blocked','reason':'camera_source_changed'}
            if not self.camera_evidence_ready(strict=self.secure):
                return {'state':'blocked','reason':'camera_transport_untrusted'}
            evidence_ts=event.get('evidence_ts')
            fresh=(isinstance(evidence_ts,(int,float)) and math.isfinite(evidence_ts)
                   and 0<=time.time()-evidence_ts<10 and event.get('evidence_source')=='secure_live_snapshot'
                   and not event.get('event_ended'))
            if not self.transport or not fresh or event.get('liveness_confirmed') is not True or not event.get('event_id'):
                if not self.transport:reason='connector_disabled'
                elif not event.get('event_id'):reason='event_id_missing'
                elif event.get('evidence_source')!='secure_live_snapshot':reason='evidence_not_live'
                elif event.get('event_ended'):reason='event_already_ended'
                elif not fresh:reason='evidence_expired_or_missing'
                else:reason='liveness_unconfirmed'
                return {'state':'blocked','reason':reason}
            expires_at=evidence_ts+10
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
            'person_id':person_id,'camera_id':camera_id,'recognized_at':stamp(evidence_ts),'expires_at':stamp(expires_at),
            'recognition':{'score':event.get('score'),'metric':'face_embedding_similarity'},
            'liveness':{'status':'passed','method':'rgb_passive','score':event.get('liveness_score'),
                        'confirmed_frames':event.get('liveness_frames',0)}}
        with self.audit._connection() as con:
            try:con.execute('INSERT INTO access1_attempts VALUES(?,?,?,?,?,?)',
                (request_id,event['event_id'],time.time(),'pending',json.dumps(payload),None))
            except sqlite3.IntegrityError:return {'state':'duplicate','request_id':request_id}
        result=None
        failure_reason=None
        transport=self.transport
        started=time.perf_counter()
        if stream:
            remaining,reason=manager.evidence(event)
            with self._lock:
                unchanged=(self.config['cameras'].get(event.get('camera'))==camera_id and self.person_id(event.get('slug'))==person_id)
            if remaining is None or not unchanged or time.time()>=expires_at:
                reason=reason if remaining is None else 'mapping_or_expiry_changed'
                with self.audit._connection() as con:
                    con.execute('UPDATE access1_attempts SET state=?,result=? WHERE request_id=?',('blocked',json.dumps({'reason':reason}),request_id))
                return {'state':'blocked','reason':reason,'request_id':request_id}
        try:result=transport.call('POST',request_id,payload)
        except Exception as exc:failure_reason=self._result_failure(exc)
        self._timing(event,'access1_post',started)
        job={'event':event,'request_id':request_id,'result':result,
             'failure_reason':failure_reason,'transport':transport,
             'deadline':min(expires_at,time.time()+8),
             'monotonic_deadline':time.monotonic()+max(0,min(expires_at-time.time(),8)),
             'started':started}
        if defer and self._pending(result):
            try:
                self._result_queue.put_nowait(job)
                return {'state':'pending','request_id':request_id,'reason':'access1_result_pending'}
            except queue.Full:
                job['failure_reason']='access1_result_queue_full'
                return self._finish_result(job)
        while True:
            summary=self._poll_result(job)
            if summary is not None:return summary

    @staticmethod
    def _pending(result):
        return result is None or result.get('status') in ('pending','dispatched')

    def _timing(self,event,stage,started):
        seconds=max(0,time.perf_counter()-started)
        recorder=getattr(self.processor,'record_timing',None)
        if callable(recorder):recorder(stage,seconds)
        if getattr(self.processor,'timing_diagnostics',False) is True:
            log.info('timing event=%s stage=%s elapsed_ms=%.1f',event.get('event_id'),stage,seconds*1000)

    def _poll_result(self,job):
        if not self._pending(job['result']):return self._finish_result(job)
        # Reconciliation is GET-only and bound to the original connector.
        # Re-pairing must never send an old request to a new destination.
        if self.transport is not job['transport']:
            job.update(result=None,failure_reason='access1_connector_changed')
            return self._finish_result(job)
        remaining=min(job['deadline']-time.time(),job['monotonic_deadline']-time.monotonic())
        if remaining<=0:return self._finish_result(job)
        time.sleep(min(.3,remaining))
        if time.time()>=job['deadline'] or time.monotonic()>=job['monotonic_deadline']:
            return self._finish_result(job)
        started=time.perf_counter()
        try:job['result']=job['transport'].call('GET',job['request_id'])
        except Exception as exc:
            job['failure_reason']=self._result_failure(exc)
            return self._finish_result(job)
        finally:self._timing(job['event'],'access1_get',started)
        return None if self._pending(job['result']) else self._finish_result(job)

    def _result_worker(self):
        while True:self._result_once()

    def _result_once(self):
        job=self._result_queue.get()
        try:
            result=self._poll_result(job)
            if result is None:
                try:self._result_queue.put_nowait(job)
                except queue.Full:
                    job['failure_reason']='access1_result_queue_full'
                    result=self._finish_result(job)
            if result is not None:self._record_submission(job['event'],result)
        except Exception:
            log.warning('Access1 result tracking unavailable; no automatic replay')
            job.update(result=None,failure_reason='access1_result_tracking_failed')
            try:self._record_submission(job['event'],self._finish_result(job))
            except Exception:log.warning('Access1 result could not be persisted; access remains unconfirmed')
        finally:self._result_queue.task_done()

    def _finish_result(self,job):
        result,request_id,failure_reason=job['result'],job['request_id'],job['failure_reason']
        self._timing(job['event'],'access1_result_total',job['started'])
        state=result.get('status') if result else 'unconfirmed'
        if state in ('pending','dispatched'):state='unconfirmed'
        with self._lock:
            with self.audit._connection() as con:
                con.execute('UPDATE access1_attempts SET state=?,result=? WHERE request_id=?',(state,json.dumps(result),request_id))
        summary={'state':state,'request_id':request_id}
        if result:
            reason=result.get('reason')
            if isinstance(reason,str) and re.fullmatch(r'[A-Za-z0-9_:-]{1,120}',reason):
                summary['reason']=reason
            for key in ('execution_confirmed','door_open_confirmed'):
                if type(result.get(key)) is bool:summary[key]=result[key]
        if state=='unconfirmed':
            summary.setdefault('reason',failure_reason or 'access1_result_pending')
        return summary

    @staticmethod
    def _result_failure(exc):
        # Categorize locally; never log exception text containing credentials/body.
        message=str(exc)
        if re.fullmatch(r'access1_http_[0-9]{3}',message):return message
        if message=='access1_response_oversized':return message
        if isinstance(exc,ValueError):return 'access1_result_invalid'
        return 'access1_transport_unavailable'

    def recent(self):
        with self.audit._connection() as con:
            con.row_factory=sqlite3.Row
            return [dict(row) for row in con.execute('SELECT request_id,face_event_id,ts,state,result FROM access1_attempts ORDER BY ts DESC LIMIT 100')]


def register_access1_routes(app,processor,gallery,data_dir):
    from fastapi import Depends, Response
    from .access1_admin import require_pairing_admin
    integration=getattr(processor,'access1',None)
    if integration is None:return
    from pydantic import BaseModel, ConfigDict, Field, SecretStr, StrictBool
    class SecuritySettings(BaseModel):
        model_config=ConfigDict(extra='forbid')
        secure:StrictBool
        risk_acknowledged:StrictBool=False
    @app.post('/api/access1/security')
    def security_settings(body:SecuritySettings,response:Response,user_id=Depends(require_pairing_admin)):
        from fastapi import HTTPException
        response.headers['Cache-Control']='no-store'
        try:return integration.set_security(body.secure,body.risk_acknowledged,user_id)
        except ValueError as error:raise HTTPException(400,str(error)) from None

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
    @app.get('/api/access1/connection')
    def access1_connection(response:Response):
        response.headers['Cache-Control']='no-store'
        return integration.connection_status()

    @app.get('/api/access1')
    def access1_status():return {**integration.status(),'attempts':integration.recent()}
