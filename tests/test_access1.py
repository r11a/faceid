import asyncio
from contextlib import contextmanager
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from app.access1 import Access1Integration
from app.access1_transport import Transport


class Audit:
    def __init__(self,path):self.path=path
    @contextmanager
    def _connection(self):
        con=sqlite3.connect(self.path)
        try:
            with con:yield con
        finally:con.close()


class Gallery:
    def __init__(self,path):
        self.persons_dir=path/'persons';self.persons_dir.mkdir();(self.persons_dir/'alice').mkdir()
        self.assignment_lock=threading.RLock();self.name='Alice'
    def persons(self):return {'alice':{'name':self.name}}


class Access1Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.gallery=Gallery(self.root);self.audit=Audit(self.root/'audit.db');self.processor=Mock()
        self.processor.frigate.base='https://frigate.internal:8971'
        self.processor.frigate.secure_mode=True
        self.processor.frigate.verify_tls=True
        self.processor.frigate.session.verify=True
        self.processor.frigate.username='frigate-user'
        self.processor.frigate.password='frigate-password'
        self.processor.frigate._authenticated=True
        self.response=Mock(status_code=200);self.response.json.return_value={'cameras':{'front':{}}}
        self.processor.frigate.request.return_value=self.response;self.processor.camera_enabled.return_value=True
        self.integration=Access1Integration(self.root,self.audit,gallery=self.gallery,processor=self.processor)
    def event(self,**changes):
        return dict(event_id='frigate-event',slug='alice',camera='front',score=.8,evidence_ts=time.time(),
            evidence_source='secure_live_hires',liveness_confirmed=True,liveness_score=.9,liveness_frames=3,
            person_id=self.integration.person_id('alice'),camera_id=self.integration.config['cameras'].get('front'),**changes)
    def ready(self):
        self.integration.directory();self.integration.transport=Mock()
        self.integration.transport.call.return_value={'status':'accepted'}
    def test_disabled_default(self):
        self.integration.start();self.assertIsNone(self.integration.server)
        self.assertEqual(self.integration.present(self.event())['state'],'blocked')
    def test_stable_person_identity_and_recreation(self):
        first=self.integration.person_id('alice');self.gallery.name='Renamed'
        self.assertEqual(first,self.integration.directory()['people'][0]['id'])
        shutil.rmtree(self.gallery.persons_dir/'alice');(self.gallery.persons_dir/'alice').mkdir()
        self.assertNotEqual(first,self.integration.person_id('alice'))
    def test_directory_has_no_credentials_images_or_users(self):
        value=self.integration.directory()
        self.assertEqual(set(value),{'schema_version','instance_id','people','cameras'})
        self.assertEqual(set(value['people'][0]),{'id','name'})
        first=value['cameras'][0]['id'];self.response.status_code=503
        with self.assertRaises(ValueError):self.integration.directory()
        self.response.status_code=200;self.assertEqual(first,self.integration.directory()['cameras'][0]['id'])
        self.response.json.return_value={'cameras':{}};self.integration.directory()
        self.response.json.return_value={'cameras':{'front':{}}}
        self.assertNotEqual(first,self.integration.directory()['cameras'][0]['id'])
    def test_unconfigured_frigate_empty_directory_has_no_access_side_effects(self):
        self.processor.frigate.base=''
        self.gallery.persons=Mock(return_value={})
        self.integration.transport=Mock()
        value=self.integration.directory()
        self.assertEqual(value,{'schema_version':'1.0','instance_id':self.integration.config['instance_id'],
                                'people':[],'cameras':[]})
        self.processor.frigate.request.assert_not_called()
        self.integration.transport.call.assert_not_called()
        self.assertEqual(self.integration.recent(),[])
        self.assertEqual(self.integration._queue.qsize(),0)

    def test_unconfigured_frigate_preserves_people_and_camera_identity(self):
        original=self.integration.directory()
        self.processor.frigate.request.reset_mock()
        self.processor.frigate.base='  '
        empty=self.integration.directory()
        self.assertEqual(empty['cameras'],[])
        self.assertEqual(empty['people'],original['people'])
        self.processor.frigate.request.assert_not_called()
        self.processor.frigate.base='https://frigate.internal:8971'
        self.assertEqual(self.integration.directory(),original)

    def test_configured_frigate_errors_are_not_an_empty_directory(self):
        for status in (401,403,503):
            with self.subTest(status=status):
                self.response.status_code=status
                with self.assertRaises(ValueError):self.integration.directory()
        self.response.status_code=200
        self.response.json.return_value={'cameras':None}
        with self.assertRaises(ValueError):self.integration.directory()
        self.processor.frigate.request.side_effect=TimeoutError()
        with self.assertRaises(TimeoutError):self.integration.directory()

    def test_camera_disable_hides_directory_entry_without_rotating_identity(self):
        original = self.integration.directory()['cameras'][0]['id']
        self.processor.camera_enabled.return_value = False
        self.assertEqual(self.integration.directory()['cameras'], [])
        self.processor.camera_enabled.return_value = True
        self.assertEqual(self.integration.directory()['cameras'][0]['id'], original)

    def test_one_event_no_duplicate_or_policy_fields(self):
        self.ready();event=self.event();self.assertEqual(self.integration.present(event)['state'],'accepted')
        self.assertEqual(self.integration.present(event)['state'],'duplicate')
        self.integration.transport.call.assert_called_once()
        payload=self.integration.transport.call.call_args.args[2]
        self.assertEqual(payload['liveness']['confirmed_frames'],3)
        self.assertFalse({'user_id','reader_id','duration','relay','allow'} & payload.keys())
    def test_lost_response_only_queries_result(self):
        self.ready();self.integration.transport.call.side_effect=[TimeoutError(),{'status':'accepted'}]
        self.assertEqual(self.integration.present(self.event())['state'],'accepted')
        self.assertEqual([call.args[0] for call in self.integration.transport.call.call_args_list],['POST','GET'])
    def test_stale_and_untrusted_evidence_never_submitted(self):
        self.ready()
        for changes in ({'evidence_ts':time.time()-11},{'evidence_source':'snapshot'},
                        {'evidence_source':'clip:12'},
                        {'liveness_confirmed':False},{'event_ended':True}):
            event=self.event();event.update(changes);self.assertEqual(self.integration.present(event)['state'],'blocked')
        self.integration.transport.call.assert_not_called()

    def test_unknown_person_or_camera_never_submits_identity(self):
        self.ready()
        event = self.event()
        event['camera'] = 'unmapped-camera'
        self.assertEqual(self.integration.present(event)['state'], 'blocked')
        event = self.event()
        event['slug'] = 'unknown-person'
        with self.assertLogs('faceid.access1', level='WARNING'):
            self.integration.enqueue(event)
        self.assertTrue(self.integration._queue.empty())
        self.integration.transport.call.assert_not_called()

    def test_secure_label_cannot_bypass_actual_camera_transport_and_login(self):
        self.ready()
        frigate = self.processor.frigate
        for attribute, unsafe in (
            ('base', 'http://frigate.internal:5000'), ('secure_mode', False),
            ('verify_tls', False), ('_authenticated', False),
            ('username', ''), ('password', ''),
        ):
            with self.subTest(attribute=attribute):
                self.integration.transport.call.reset_mock()
                original = getattr(frigate, attribute)
                setattr(frigate, attribute, unsafe)
                try:
                    event = self.event()
                    event['event_id'] = f'unsafe-{attribute}'
                    result = self.integration.present(event)
                    self.assertEqual(result['state'], 'blocked')
                    self.integration.transport.call.assert_not_called()
                finally:
                    setattr(frigate, attribute, original)
        for index, unsafe in enumerate((False, None, '')):
            with self.subTest(session_verify=unsafe):
                frigate.session.verify = unsafe
                self.integration.transport.call.reset_mock()
                event = self.event()
                event['event_id'] = f'unsafe-session-verify-{index}'
                self.assertEqual(self.integration.present(event)['state'], 'blocked')
                self.integration.transport.call.assert_not_called()

    def test_camera_disabled_after_enqueue_never_submits_queued_identity(self):
        self.ready()
        self.integration.enqueue(self.event())
        queued = self.integration._queue.get_nowait()
        self.processor.camera_enabled.return_value = False
        self.assertEqual(self.integration.present(queued)['state'], 'blocked')
        self.integration.transport.call.assert_not_called()
    def test_restart_does_not_replay(self):
        self.ready();self.integration.present(self.event())
        restarted=Access1Integration(self.root,self.audit,gallery=self.gallery,processor=self.processor)
        self.assertEqual(restarted.config['instance_id'],self.integration.config['instance_id'])
        self.assertEqual(restarted._queue.qsize(),0)

    def test_recreated_person_cannot_retarget_queued_event(self):
        self.ready();self.integration.enqueue(self.event());queued=self.integration._queue.get_nowait()
        shutil.rmtree(self.gallery.persons_dir/'alice');(self.gallery.persons_dir/'alice').mkdir()
        self.assertEqual(self.integration.present(queued)['state'],'blocked')
        self.integration.transport.call.assert_not_called()
    def test_wildcard_and_public_listener_forbidden(self):
        for host in ('0.0.0.0','::','127.0.0.1','8.8.8.8','169.254.169.254'):
            self.integration.options={'bind_address':host}
            with self.assertRaises(ValueError):self.integration.start_directory()
    def test_directory_auth_is_separate(self):
        self.integration.options={'directory_token':'directory-test-'+'x'*32}
        app=self.integration.directory_app()
        async def call(token):
            messages=[]
            async def receive():return {'type':'http.request','body':b'','more_body':False}
            async def send(value):messages.append(value)
            await app(dict(type='http',asgi={'version':'3.0'},http_version='1.1',method='GET',scheme='https',
                path='/access1/v1/directory',raw_path=b'/access1/v1/directory',query_string=b'',
                headers=[(b'authorization',('Bearer '+token).encode())],server=('fixture',8601),client=('127.0.0.1',1),root_path=''),receive,send)
            return messages[0]['status']
        self.assertEqual(asyncio.run(call('acs_face_wrong_service_token')),401)
        self.assertEqual(asyncio.run(call(self.integration.options['directory_token'])),200)
        self.processor.frigate.base=''
        self.gallery.persons=Mock(return_value={})
        self.assertEqual(asyncio.run(call('wrong')),401)
        self.assertEqual(asyncio.run(call(self.integration.options['directory_token'])),200)
        self.processor.frigate.base='https://frigate.internal:8971'
        self.response.status_code=503
        self.assertEqual(asyncio.run(call(self.integration.options['directory_token'])),503)

    def test_connection_check_uses_separate_auth_and_never_presents_identity(self):
        self.integration.options={'directory_token':'directory-test-'+'x'*32}
        self.integration.transport=Mock()
        self.integration.transport.check_connection.return_value={
            'instance_id':self.integration.config['instance_id'],
            'status':'connection_verified','requests_enabled':False}
        app=self.integration.directory_app()
        async def call(token):
            messages=[]
            async def receive():return {'type':'http.request','body':b'','more_body':False}
            async def send(value):messages.append(value)
            await app(dict(type='http',asgi={'version':'3.0'},http_version='1.1',method='GET',scheme='https',
                path='/access1/v1/connection',raw_path=b'/access1/v1/connection',query_string=b'',
                headers=[(b'authorization',('Bearer '+token).encode())],server=('fixture',8601),client=('127.0.0.1',1),root_path=''),receive,send)
            return messages[0]['status']
        self.assertEqual(asyncio.run(call('wrong')),401)
        self.integration.transport.check_connection.assert_not_called()
        self.assertEqual(asyncio.run(call(self.integration.options['directory_token'])),200)
        self.integration.transport.check_connection.assert_called_once_with(self.integration.config['instance_id'])
        self.integration.transport.call.assert_not_called()
        self.assertEqual(self.integration.recent(),[])
        self.integration.transport.check_connection.side_effect=ValueError('PRIVATE')
        self.assertEqual(asyncio.run(call(self.integration.options['directory_token'])),502)
        self.integration.transport=None
        self.assertEqual(asyncio.run(call(self.integration.options['directory_token'])),409)

    def test_transport_connection_check_validates_identity_and_does_not_post(self):
        from uuid import uuid4
        instance=str(uuid4())
        with patch('app.access1_transport.ssl.create_default_context'),patch('app.access1_transport.http.client.HTTPSConnection') as connection:
            response=connection.return_value.getresponse.return_value
            response.status=200
            response.read.return_value=json.dumps({'instance_id':instance,'status':'connection_verified','requests_enabled':False}).encode()
            transport=Transport('https://backend.internal:8445','acs_face_'+'x'*40,'fixture-ca')
            self.assertFalse(transport.check_connection(instance)['requests_enabled'])
            self.assertEqual(connection.return_value.request.call_args.args,('GET','/faceid/v1/connection'))
            with self.assertRaises(ValueError):transport.check_connection(str(uuid4()))
            response.status=403
            with self.assertRaises(ValueError):transport.check_connection(instance)
    def test_old_history_is_preserved_not_executable(self):
        with self.audit._connection() as con:
            con.execute('DROP TABLE access1_attempts')
            con.execute('CREATE TABLE access1_attempts(request_id TEXT,face_event_id TEXT,user_id TEXT,reader_id TEXT,ts REAL,state TEXT)')
            con.execute("INSERT INTO access1_attempts VALUES('old','event','user','reader',1,'pending')")
        migrated=Access1Integration(self.root,self.audit,gallery=self.gallery,processor=self.processor)
        self.assertEqual(migrated.recent()[0]['state'],'historical')
        self.assertEqual(json.loads(migrated.recent()[0]['result'])['user_id'],'user')

    def test_transport_tls_and_result_contract(self):
        from uuid import uuid4
        event=str(uuid4());token='acs_face_'+'x'*40
        with patch('app.access1_transport.ssl.create_default_context') as tls,patch('app.access1_transport.http.client.HTTPSConnection') as connection:
            transport=Transport('https://access1.internal:8445',token,'/ssl/ca.pem')
            tls.assert_called_once_with(cafile='/ssl/ca.pem')
            response=connection.return_value.getresponse.return_value;response.status=200
            response.read.return_value=json.dumps({'event_id':event,'source':'faceid','status':'accepted','execution_confirmed':True}).encode()
            self.assertEqual(transport.call('POST',event,{'event_id':event})['status'],'accepted')
            self.assertEqual(connection.call_args.kwargs['context'],tls.return_value)
            self.assertEqual(connection.return_value.request.call_args.kwargs['headers']['Authorization'],'Bearer '+token)
            response.status=302
            with self.assertRaises(ValueError):transport.call('POST',event,{})
            response.status=200;response.read.return_value=b'{}'
            with self.assertRaises(ValueError):transport.call('GET',event)
        with self.assertRaises(ValueError):Transport('http://access1',token,None)


if __name__=='__main__':unittest.main()
