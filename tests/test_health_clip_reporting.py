import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi.testclient import TestClient
from app.webui import build_app
from app.mqtt_listener import EventProcessor
from app.frigate_api import FrigateAPI

class ReportingTests(unittest.TestCase):
    def client(self, root, status=200, event_status='unknown'):
        upstream=Mock(status_code=status, headers={})
        upstream.iter_content.return_value=iter([b'video'])
        processor=SimpleNamespace(
            audit=SimpleNamespace(event_detail=lambda _: {'event': {'event_id':'e1','status':event_status,'end_ts':time.time()}}),
            media_store=None, frigate=SimpleNamespace(timeout=1,request=Mock(return_value=upstream)))
        app=build_app({'faceid':{'auth':{}}},SimpleNamespace(),SimpleNamespace(persons=lambda:{}),processor,root,Path(__file__).parents[1]/'static')
        return TestClient(app),upstream,processor

    def test_mqtt_health_reads_client_state_not_webhooks(self):
        for connected in [True,False]:
            processor=SimpleNamespace(client=SimpleNamespace(is_connected=lambda:connected))
            self.assertEqual(EventProcessor.mqtt_health(processor),{'connected':connected})
        self.assertEqual(EventProcessor.mqtt_health(SimpleNamespace(client=None)),{'connected':False})

    def test_mqtt_rejection_never_announces_success(self):
        processor=SimpleNamespace(_publish_discovery=Mock())
        client=Mock()
        EventProcessor._on_connect(processor,client,None,None,SimpleNamespace(is_failure=True),None)
        client.subscribe.assert_not_called();client.publish.assert_not_called()
        processor._publish_discovery.assert_not_called()

    def test_successful_frigate_probe_separates_tls_off_from_unreachable(self):
        f=FrigateAPI('https://fixture.invalid:8971',username='fixture',password='fixture',verify_tls=False)
        f._authenticated=True;f.request=Mock(return_value=Mock(status_code=200))
        result=f.connection_status()
        self.assertTrue(result['reachable']);self.assertTrue(result['authenticated'])
        self.assertFalse(result['tls_verified'])
        f.request.return_value.status_code=403
        self.assertFalse(f.connection_status()['authenticated'])

    def test_clip_errors_preserve_reason_and_close_upstream(self):
        for code,expected,message in [(400,409,'segments'),(401,502,'denied'),(403,502,'denied'),(404,404,'not available yet'),(500,502,'prepare'),(503,502,'prepare')]:
            with self.subTest(code=code),tempfile.TemporaryDirectory() as tmp:
                client,upstream,_=self.client(Path(tmp),code)
                response=client.get('/api/activity/e1/clip')
                self.assertEqual(response.status_code,expected)
                self.assertIn(message,response.json()['detail'])
                self.assertIn(str(code),response.json()['detail'])
                upstream.close.assert_called_once()

    def test_probe_does_not_consume_media_and_only_retries_recording_readiness(self):
        for code in [200,400,401,403,404,500]:
            with self.subTest(code=code),tempfile.TemporaryDirectory() as tmp:
                client,upstream,processor=self.client(Path(tmp),code,'processing')
                response=client.get('/api/activity/e1/clip/status?probe=true')
                self.assertEqual(response.status_code,200)
                report=response.json()
                self.assertEqual(report['upstream_http'],code)
                self.assertEqual(report['retryable'],code in [400,404])
                upstream.iter_content.assert_not_called();upstream.close.assert_called_once()
                self.assertEqual(processor.frigate.request.call_args.kwargs['headers']['Range'],'bytes=0-0')

    def test_cached_clip_diagnostic_does_not_blame_or_contact_frigate(self):
        with tempfile.TemporaryDirectory() as tmp:
            client,upstream,processor=self.client(Path(tmp),403)
            processor.media_store=SimpleNamespace(cached_clip_path=lambda _:Path(tmp)/'cached.mp4')
            result=client.get('/api/activity/e1/clip/status?probe=true').json()
            self.assertTrue(result['cached']);self.assertTrue(result['ready'])
            self.assertFalse(result['retryable'])
            processor.frigate.request.assert_not_called()
