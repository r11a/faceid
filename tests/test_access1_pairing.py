import os
import base64
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, Mock
from uuid import uuid4
from fastapi import HTTPException
from app import access1_pairing as pairing


class PairingTests(unittest.TestCase):
    def setUp(self):
        self.instance=str(uuid4())
        self.value={'version':1,'instance_id':self.instance,'session':str(uuid4()),
                    'origin':'https://local-acs-bench:8445','secret':'a'*43,'certificate':'fixture'}

    def code(self,value=None):
        return 'A1P1.'+base64.urlsafe_b64encode(json.dumps(value or self.value).encode()).decode()

    def test_decode_only_expected_identity_private_backend_format_and_tls(self):
        with patch.object(pairing.ssl,'create_default_context') as context:
            self.assertEqual(pairing.decode(self.code(),self.instance),self.value)
            context.assert_called_once_with(cadata='fixture')
        for field,value in [('origin','http://local-acs-bench:8445'),
                            ('origin','https://local-acs-bench:8445/admin'),
                            ('origin','https://user:password@local-acs-bench:8445'),
                            ('instance_id',str(uuid4())),('secret','123456'),('version',2)]:
            with self.assertRaises(HTTPException):
                pairing.decode(self.code({**self.value,field:value}),self.instance)
        with self.assertRaises(HTTPException):pairing.decode(self.code(),self.instance)

    def test_exchange_never_sends_secret_to_public_address(self):
        with patch.object(pairing.socket,'getaddrinfo',return_value=[(None,None,None,None,('8.8.8.8',8445))]), \
                patch.object(pairing.http.client,'HTTPSConnection') as connection:
            with self.assertRaises(HTTPException):pairing.exchange(self.value,'POST','secret',{})
            connection.assert_not_called()

    def test_dual_stack_dns_uses_only_ipv4_and_pins_validated_address(self):
        import socket
        def resolve(host,port,**kwargs):
            self.assertEqual((host,port),('local-acs-bench',8445))
            records=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('172.30.33.3',8445)),
                     (socket.AF_INET6,socket.SOCK_STREAM,6,'',('fd0c:ac1e:2100::a',8445,0,0))]
            return [r for r in records if r[0]==kwargs.get('family',socket.AF_UNSPEC)] if kwargs.get('family') else records
        with patch.object(pairing.socket,'getaddrinfo',side_effect=resolve) as dns, \
             patch.object(pairing.ssl,'create_default_context') as context, \
             patch.object(pairing.http.client,'HTTPSConnection') as factory, \
             patch.object(pairing.socket,'create_connection') as connect:
            response=Mock(status=200);response.read.return_value=b'{"state":"review"}'
            factory.return_value.getresponse.return_value=response
            self.assertEqual(pairing.exchange(self.value,'POST','fixture',{}),{'state':'review'})
            dns.assert_called_once_with('local-acs-bench',8445,family=socket.AF_INET,type=socket.SOCK_STREAM)
            factory.assert_called_once_with('local-acs-bench',8445,timeout=4,context=context.return_value)
            factory.return_value._create_connection(('ignored-host',8445),4,None)
            connect.assert_called_once_with(('172.30.33.3',8445),4,None)

    def test_ipv4_no_answers_or_mixed_public_answers_fail_closed(self):
        import socket
        for addresses in ([],['172.30.33.3','8.8.8.8'],['127.0.0.1'],['169.254.169.254'],['fd0c:ac1e:2100::a']):
            with self.subTest(addresses=addresses), \
                patch.object(pairing.socket,'getaddrinfo',return_value=[(socket.AF_INET,1,6,'',(a,8445)) for a in addresses]), \
                patch.object(pairing.http.client,'HTTPSConnection') as factory:
                with self.assertRaises(HTTPException):pairing.exchange(self.value,'POST','fixture',{})
                factory.assert_not_called()

    def test_exchange_failure_diagnostics_never_include_sensitive_material(self):
        import socket,ssl
        cases=[
            ('dns',socket.gaierror('PRIVATE_TOKEN'), 'dns','dns_failed'),
            ('connect',ConnectionRefusedError('PRIVATE_TOKEN'),'connect','connection_refused'),
            ('connect',TimeoutError('PRIVATE_TOKEN'),'connect','timeout'),
            ('connect',ssl.SSLCertVerificationError('PRIVATE_TOKEN'),'tls','certificate_verification_failed'),
            ('connect',ssl.SSLError('PRIVATE_TOKEN'),'tls','tls_failed'),
            ('request',OSError('PRIVATE_TOKEN'),'request','network_failed'),
            ('response',TimeoutError('PRIVATE_TOKEN'),'response','timeout'),
        ]
        for where,failure,stage,reason in cases:
            with self.subTest(where=where,reason=reason), \
                patch.object(pairing.socket,'getaddrinfo',return_value=[(0,0,0,0,('172.30.33.1',8445))]) as dns, \
                patch.object(pairing.ssl,'create_default_context'), \
                patch.object(pairing.http.client,'HTTPSConnection') as factory, \
                self.assertLogs(pairing.__name__,level='WARNING') as logs:
                connection=factory.return_value
                target={'dns':dns,'connect':connection.connect,'request':connection.request,'response':connection.getresponse}[where]
                target.side_effect=failure
                with self.assertRaises(HTTPException) as error:
                    pairing.exchange(self.value,'POST','PRIVATE_TOKEN',{'secret':'PRIVATE_BODY'})
                self.assertEqual(error.exception.detail,f'Access1 pairing: stage={stage} reason={reason}')
                self.assertNotIn('PRIVATE',str(logs.output)+str(error.exception.detail))
                self.assertNotIn(self.value['origin'],str(logs.output))
                if where!='dns':connection.close.assert_called_once()

    def test_exchange_http_and_response_diagnostics_and_success(self):
        for status,raw,reason in [(401,b'PRIVATE_TOKEN','http_401'),(403,b'PRIVATE_TOKEN','http_403'),
            (404,b'PRIVATE_TOKEN','http_404'),(410,b'PRIVATE_TOKEN','http_410'),
            (500,b'PRIVATE_TOKEN','http_500'),(200,b'x'*16385,'response_too_large'),
            (200,b'PRIVATE_TOKEN','invalid_response'),(200,b'[]','invalid_response'),
            (200,b'{"state":"review"}',None)]:
            with self.subTest(status=status,reason=reason), \
                patch.object(pairing.socket,'getaddrinfo',return_value=[(0,0,0,0,('172.30.33.1',8445))]), \
                patch.object(pairing.ssl,'create_default_context') as context, \
                patch.object(pairing.http.client,'HTTPSConnection') as factory:
                response=Mock(status=status);response.read.return_value=raw
                factory.return_value.getresponse.return_value=response
                if reason:
                    with self.assertLogs(pairing.__name__,level='WARNING') as logs,self.assertRaises(HTTPException) as error:
                        pairing.exchange(self.value,'POST','PRIVATE_TOKEN',{})
                    self.assertTrue(error.exception.detail.endswith('reason='+reason))
                    self.assertEqual(error.exception.status_code,410 if status==410 else 502)
                    self.assertNotIn('PRIVATE_TOKEN',str(logs.output)+error.exception.detail)
                    if status!=200:response.read.assert_not_called()
                else:
                    self.assertEqual(pairing.exchange(self.value,'POST','PRIVATE_TOKEN',{}),{'state':'review'})
                    context.assert_called_once_with(cadata=self.value['certificate'])
                    factory.return_value.connect.assert_called_once()

    def test_expiry_and_idle_do_not_start_connector(self):
        integration=SimpleNamespace(_lock=__import__('threading').RLock(),transport=None)
        self.assertEqual(pairing.finish(integration),{'state':'idle'})
        integration._pairing={'deadline':0}
        with self.assertRaises(HTTPException) as error:pairing.finish(integration)
        self.assertEqual(error.exception.status_code,410)
        self.assertIsNone(integration._pairing)

    def test_startup_loads_container_environment_without_credential_fallback_files(self):
        script=Path(os.environ.get('FACEID_TEST_ROOT', Path(__file__).resolve().parents[1]))/'faceid-addon/run.sh'
        text=script.read_text()
        self.assertEqual(text.splitlines()[0],'#!/usr/bin/with-contenv bashio')
        self.assertNotIn('/proc/1/environ',text)
        self.assertNotIn('/run/s6/container_environment',text)


class PersistenceTests(unittest.TestCase):
    def test_approved_setup_is_saved_once_and_survives_options_regeneration(self):
        import tempfile,threading,time
        from unittest.mock import Mock
        from app.access1 import Access1Integration
        from tests.test_access1 import Audit
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);(root/'access1-wizard-tls').mkdir()
            integration=Access1Integration(root,Audit(root/'audit.db'),options={'enabled':False})
            instance=integration.config['instance_id']
            integration._pairing={'session':str(uuid4()),'receipt':'r'*43,'deadline':time.time()+100,
                'certificate':'public-only','origin':'https://local-acs-bench:8445',
                'options':{'enabled':True,'directory_token':'d'*43}}
            token='acs_face_'+'t'*43
            with patch.object(pairing,'exchange',return_value={'state':'approved','instance_id':instance,'token':token}), \
                    patch('app.access1_transport.Transport') as transport,patch.object(integration,'start_worker') as worker, \
                    patch('app.access1.os.chmod', wraps=os.chmod) as chmod:
                self.assertEqual(pairing.finish(integration),{'state':'connected'})
                transport.return_value.check_connection.assert_called_once_with(instance)
                worker.assert_called_once()
            saved=root/'access1-connector.json'
            self.assertTrue(any(call.args[0].name.startswith('access1-connector.json.')
                                and call.args[1] == 0o600 for call in chmod.call_args_list))
            if os.name == 'posix':
                self.assertEqual(saved.stat().st_mode & 0o777,0o600)
            restarted=Access1Integration(root,Audit(root/'audit.db'),options={'enabled':False,'access1_url':'stale-option'})
            self.assertTrue(restarted.options['paired'])
            self.assertEqual(restarted.options['access1_token'],token)
            self.assertEqual(restarted.config['instance_id'],instance)
            pairing.disconnect(restarted)
            restarted=Access1Integration(root,Audit(root/'audit.db'),options={'enabled':True})
            self.assertFalse(restarted.options['enabled'])

    def test_background_finishes_after_browser_navigation_without_access_request(self):
        from unittest.mock import Mock
        integration=Mock()
        with patch.object(pairing.time,'sleep'),patch.object(pairing,'finish',return_value={'state':'connected'}) as finish:
            pairing.complete_in_background(integration,'session')
            finish.assert_called_once_with(integration,expected_session='session')
        integration.present.assert_not_called()
        integration.enqueue.assert_not_called()

    def test_unexpected_setup_failure_is_terminal_and_does_not_leak_error(self):
        import threading
        integration=SimpleNamespace(_lock=threading.RLock(),_pairing={'session':'session'},state='pairing_review')
        with patch.object(pairing.time,'sleep'),patch.object(pairing,'finish',side_effect=OSError('private fixture')):
            pairing.complete_in_background(integration,'session')
        self.assertEqual(integration.state,'pairing_failed')
        self.assertIsNone(integration._pairing)

    def test_poll_does_not_hold_directory_lock_while_waiting_for_access1_approval(self):
        import threading,time
        integration=SimpleNamespace(_lock=threading.RLock(),transport=None,
            _pairing={'session':'session','receipt':'r'*43,'deadline':time.time()+60})
        acquired=[]
        def directory_callback():
            held=integration._lock.acquire(timeout=.2);acquired.append(held)
            if held:integration._lock.release()
        def reply(*args):
            worker=threading.Thread(target=directory_callback);worker.start();worker.join()
            return {'state':'review'}
        with patch.object(pairing,'exchange',side_effect=reply):
            self.assertEqual(pairing.finish(integration),{'state':'review'})
        self.assertEqual(acquired,[True])
