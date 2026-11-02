import asyncio
import ast
import json
from pathlib import Path
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, HTTPException
from starlette.requests import Request
from app import acquisition_diagnostics as module


def request(body, method='POST'):
    async def receive():
        return {'type':'http.request','body':body,'more_body':False}
    return Request({'type':'http','method':method,'path':'/api/acquisition-diagnostics',
                    'headers':[], 'client':('172.30.32.2',1)}, receive)


class InputTests(unittest.TestCase):
    def test_clock_subprocess_has_fixed_duration_and_bounded_deadline(self):
        with patch.object(module.subprocess,'Popen') as popen:
            p=popen.return_value;p.returncode=0;p.poll.return_value=0
            p.communicate.return_value=(b'{}',None)
            module.probe_source('rtsp://SECRET@10.0.0.1/a',seconds=30)
            self.assertEqual(p.communicate.call_args.kwargs['timeout'],47)
            self.assertEqual(json.loads(p.communicate.call_args.args[0])['seconds'],30)
            self.assertNotIn('SECRET',str(popen.call_args))
    def test_private_lan_only(self):
        for value in ['rtsp://u:p@10.0.0.100:554/ch01/00', 'rtsp://192.168.1.2/live',
                      'rtsp://172.16.0.4:8554/Enterance']:
            self.assertEqual(module.validate_direct(value),value)
        for value in [None, [], 'http://10.0.0.1/a','rtsp://127.0.0.1/a',
                      'rtsp://169.254.169.254/a','rtsp://8.8.8.8/a','rtsp://localhost/a',
                      'rtsp://supervisor/a','rtsp://10.0.0.1:8123/a','rtsp://10.0.0.1:bad/a',
                      'rtsp://[::1]/a','rtsp://10.0.0.1/', 'rtsp://10.0.0.1/a#x',
                      'rtsp://u:%0d%0aSecret@10.0.0.1/a','rtsp://u:Secret\n@10.0.0.1/a', 'x'*2049]:
            with self.subTest(value=str(value)[:20]), self.assertRaises(ValueError) as error:
                module.validate_direct(value)
            self.assertNotIn('Secret',str(error.exception))

    def test_worker_has_no_application_or_access_dependencies(self):
        tree=ast.parse(Path(module.__file__).with_name('acquisition_probe.py').read_text())
        imports=[n.module for n in ast.walk(tree) if isinstance(n,ast.ImportFrom)]
        self.assertNotIn('app',imports)
        source=Path(module.__file__).with_name('acquisition_probe.py').read_text()
        for forbidden in ['cv2','access1.present','requests.post','Switch.Set','ffmpeg','PASSWORD']:
            self.assertNotIn(forbidden,source)

    def test_timeout_kills_process_group(self):
        import subprocess
        with patch.object(module.subprocess,'Popen') as popen, patch.object(module.os,'killpg',create=True) as kill, patch.object(module.signal,'SIGKILL',9,create=True):
            process=popen.return_value; process.pid=123
            process.communicate.side_effect=[subprocess.TimeoutExpired('probe',29),(b'',b'')]
            process.poll.return_value=-9
            result=module.probe_source('rtsp://SECRET@10.0.0.1/a')
            self.assertEqual(result['error'],'probe_deadline')
            kill.assert_called_once_with(123,module.signal.SIGKILL)
            self.assertNotIn('SECRET',str(popen.call_args))

    def test_subprocess_failure_is_redacted(self):
        with patch.object(module.subprocess,'Popen') as popen:
            p=popen.return_value;p.returncode=1;p.poll.return_value=1
            p.communicate.return_value=(b'SECRET',None)
            self.assertNotIn('SECRET',str(module.probe_source('rtsp://SECRET@10.0.0.1/a')))


class JobTests(unittest.TestCase):
    def test_clock_profile_is_thirty_seconds_and_keeps_camera_label(self):
        from unittest.mock import Mock
        probe=Mock(return_value={})
        job=module.AcquisitionDiagnostics(probe)
        job.state['camera']='Enterance'
        job._run([('restream_clock','rtsp://frigate:8554/Enterance')])
        probe.assert_called_once_with('rtsp://frigate:8554/Enterance',seconds=30)
        self.assertEqual(job.status(None)['results'][0]['camera'],'Enterance')
    def test_sequential_single_flight_and_credentials_released(self):
        entered=threading.Event();release=threading.Event();calls=[]
        def probe(url):
            calls.append(url);entered.set();release.wait(2)
            return {'rtcp_sender_reports':1,'freshness_proven':True}
        job=module.AcquisitionDiagnostics(probe)
        sources=[('restream','first'),('direct','SECRET')]
        job.start('admin',sources);self.assertTrue(entered.wait(1))
        with self.assertRaises(HTTPException):job.start('admin',[('direct','other')])
        with self.assertRaises(HTTPException):job.status('other')
        release.set()
        for _ in range(100):
            if not job.status('admin')['running']:break
            time.sleep(.01)
        result=job.status('admin')
        self.assertFalse(result['running']);self.assertEqual(calls,['first','SECRET'])
        self.assertEqual(sources,[]);self.assertNotIn('SECRET',json.dumps(result))
        self.assertTrue(all(r['freshness_proven'] is False for r in result['results']))
        with self.assertRaises(HTTPException):job.start('admin',[])

    def test_missing_restream_and_probe_exception(self):
        job=module.AcquisitionDiagnostics(lambda _: (_ for _ in ()).throw(RuntimeError('SECRET')))
        job._run([('restream',None),('direct','secret')])
        self.assertEqual([x['error'] for x in job.status(None)['results']],['restream_not_configured','probe_failed'])
        self.assertFalse(job.status(None)['running'])


class RouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_crop_route_validates_region_and_uses_configured_source(self):
        self.processor.camera_enabled=lambda _:False
        self.processor.engine=object();self.processor.access1=None
        with patch.object(module,'require_pairing_admin',new=AsyncMock(return_value='admin')), patch.object(module.AcquisitionDiagnostics,'start') as start:
            for source in ('preview','crop_compare'):
                await self.route(request(json.dumps({'camera':'Enterance','source':source,'region':[.1,.2,.9,.8]}).encode()))
                self.assertEqual(start.call_args.args,('admin',[(source,'rtsp://frigate:8554/Enterance')]))
                self.assertEqual(start.call_args.kwargs['region'],[.1,.2,.9,.8])
            start.reset_mock()
            with self.assertRaises(HTTPException):
                await self.route(request(json.dumps({'camera':'Enterance','source':'crop_compare','region':[0,0,.01,1]}).encode()))
            start.assert_not_called()

    async def test_preview_authentication_and_no_store_headers(self):
        from app.crop_diagnostics import PreviewCache
        import numpy as np
        cache=PreviewCache();cache.put('admin',{'scene':np.zeros((10,10,3),np.uint8)})
        app=FastAPI()
        with patch('app.crop_diagnostics.PreviewCache',return_value=cache):
            module.register_acquisition_routes(app,self.processor,Path('/missing'))
        endpoint=next(r.endpoint for r in app.routes if r.path.endswith('/preview/{kind}'))
        try:
            with patch.object(module,'require_pairing_admin',new=AsyncMock(return_value='other')):
                with self.assertRaises(HTTPException):await endpoint(request(b'',method='GET'),'scene')
            with patch.object(module,'require_pairing_admin',new=AsyncMock(return_value='admin')):
                response=await endpoint(request(b'',method='GET'),'scene')
                self.assertEqual(response.headers['cache-control'],'private, no-store')
                self.assertEqual(response.headers['cross-origin-resource-policy'],'same-origin')
                self.assertEqual(response.media_type,'image/jpeg')
        finally:cache.clear()

    async def test_decode_refuses_missing_engine_before_start(self):
        self.processor.camera_enabled=lambda _:True
        self.processor.access1=SimpleNamespace(transport=object(),options={'enabled':True})
        with patch.object(module,'require_pairing_admin',new=AsyncMock(return_value='admin')), patch.object(module.AcquisitionDiagnostics,'start') as start:
            with self.assertRaises(HTTPException) as error:
                await self.route(request(json.dumps({'camera':'Enterance','source':'decode_chain'}).encode()))
            self.assertEqual(error.exception.status_code,409)
            start.assert_not_called()

    async def test_decode_is_explicit_and_uses_only_configured_restream(self):
        self.processor.camera_enabled=lambda _:False
        self.processor.engine=object();self.processor.access1=None
        with patch.object(module,'require_pairing_admin',new=AsyncMock(return_value='admin')), patch.object(module.AcquisitionDiagnostics,'start') as start:
            await self.route(request(json.dumps({'camera':'Enterance','source':'decode_chain'}).encode()))
            self.assertEqual(start.call_args.args,('admin',[('decode_chain','rtsp://frigate:8554/Enterance')]))
    def setUp(self):
        self.app=FastAPI()
        self.processor=SimpleNamespace(cameras={'Enterance'},camera_profiles=None,
            frigate=SimpleNamespace(_live_url=lambda camera:'rtsp://frigate:8554/'+camera))
        module.register_acquisition_routes(self.app,self.processor,Path('/missing'))
        self.route=next(r.endpoint for r in self.app.routes if r.path=='/api/acquisition-diagnostics' and 'POST' in r.methods)

    async def test_auth_precedes_parsing_and_network(self):
        with patch.object(module,'require_pairing_admin',new=AsyncMock(side_effect=HTTPException(403,'denied'))), patch.object(module.AcquisitionDiagnostics,'start') as start:
            with self.assertRaises(HTTPException) as error:await self.route(request(b'not-json'))
            self.assertEqual(error.exception.status_code,403);start.assert_not_called()

    async def test_invalid_inputs_redacted(self):
        for data in [b'{',b'x'*4097,json.dumps({'camera':'unknown','source':'both','direct_url':'SECRET'}).encode(),json.dumps({'camera':'Enterance','source':'direct','direct_url':'SECRET'}).encode(),b'[]']:
            with patch.object(module,'require_pairing_admin',new=AsyncMock(return_value='admin')):
                with self.assertRaises(HTTPException) as error:await self.route(request(data))
                self.assertEqual(error.exception.status_code,400);self.assertNotIn('SECRET',str(error.exception.detail))

    async def test_both_resolves_known_restream_then_direct(self):
        direct='rtsp://u:p@10.0.0.100:554/ch01/00'
        with patch.object(module,'require_pairing_admin',new=AsyncMock(return_value='admin')), patch.object(module.AcquisitionDiagnostics,'start') as start:
            result=await self.route(request(json.dumps({'camera':'Enterance','source':'both','direct_url':direct}).encode()))
            self.assertEqual(start.call_args.args,('admin',[('restream','rtsp://frigate:8554/Enterance'),('direct',direct)]))
            self.assertEqual(result['access1_submission'],'not_implemented')

    async def test_direct_does_not_contact_frigate(self):
        self.processor.frigate._live_url=lambda _:self.fail('Frigate must not be used')
        with patch.object(module,'require_pairing_admin',new=AsyncMock(return_value='admin')), patch.object(module.AcquisitionDiagnostics,'start'):
            await self.route(request(json.dumps({'camera':'Enterance','source':'direct','direct_url':'rtsp://10.0.0.100/a'}).encode()))

    async def test_clock_profile_requires_no_direct_credentials(self):
        with patch.object(module,'require_pairing_admin',new=AsyncMock(return_value='admin')), patch.object(module.AcquisitionDiagnostics,'start') as start:
            await self.route(request(json.dumps({'camera':'Enterance','source':'restream_clock'}).encode()))
            self.assertEqual(start.call_args.args,('admin',[('restream_clock','rtsp://frigate:8554/Enterance')]))
            self.assertEqual(start.call_args.kwargs,{'camera':'Enterance'})

    async def test_status_reports_server_build(self):
        status=next(r.endpoint for r in self.app.routes if r.path=='/api/acquisition-diagnostics' and 'GET' in r.methods)
        with patch.object(module,'require_pairing_admin',new=AsyncMock(return_value='admin')):
            result=await status(request(b'',method='GET'))
        self.assertEqual(result['build'],module.VERSION)

    async def test_auto_discovery_is_cached_and_response_closed(self):
        from unittest.mock import Mock
        self.processor.cameras=set()
        response=Mock(status_code=200)
        response.json.return_value={'cameras':{'Enterance':{}}}
        self.processor.frigate.request=Mock(return_value=response)
        status=next(r.endpoint for r in self.app.routes if r.path=='/api/acquisition-diagnostics' and 'GET' in r.methods)
        with patch.object(module,'require_pairing_admin',new=AsyncMock(return_value='admin')):
            for _ in range(2):
                self.assertEqual((await status(request(b'',method='GET')))['cameras'],['Enterance'])
        self.processor.frigate.request.assert_called_once_with('GET','/api/config',timeout=2)
        response.close.assert_called_once()

    async def test_auto_discovery_failure_does_not_leak_error(self):
        from unittest.mock import Mock
        self.processor.cameras=set()
        self.processor.frigate.request=Mock(side_effect=RuntimeError('SECRET'))
        status=next(r.endpoint for r in self.app.routes if r.path=='/api/acquisition-diagnostics' and 'GET' in r.methods)
        with patch.object(module,'require_pairing_admin',new=AsyncMock(return_value='admin')):
            result=await status(request(b'',method='GET'))
        self.assertEqual(result['cameras'],[])
        self.assertNotIn('SECRET',json.dumps(result))


if __name__=='__main__':unittest.main()
