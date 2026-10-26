import time
import json
import cv2
import requests
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
import numpy as np
from app.mqtt_listener import EventProcessor
from app.frigate_api import FrigateAPI
import test_access1 as baseline_tests

class LiveSamplingTests(unittest.TestCase):
    def processor(self):
        p=EventProcessor({'faceid':{}},SimpleNamespace(faces=Mock(return_value=[])),None,
            SimpleNamespace(recording_frame=Mock(return_value=None),snapshot=Mock(return_value=None),live_event_frame=Mock(return_value=None)))
        p.camera_profiles=SimpleNamespace(get=lambda _: {'enabled':True,'fast_recognition':True,
            'high_resolution':True,'mode':'standard','min_face_px':24,'night_min_face_px':24})
        p.camera_profiles.all=lambda: {'front':p.camera_profiles.get('front')}
        p.access1=SimpleNamespace(transport=object())
        st=p._new_event_state('e','front',start_time=time.time()-8)
        st['live_updated_at']=time.time();st['live_snapshot_ts']=time.time()-1;p.events['e']=st
        return p,st

    def test_retries_without_another_mqtt_message_and_coalesces(self):
        p,st=self.processor();p._schedule_live_jobs();p._schedule_live_jobs()
        self.assertEqual(p.queue.qsize(),1)
        p._work_once(p.queue)
        st['last_try']=time.time()-1;p._schedule_live_jobs()
        self.assertEqual(p.queue.qsize(),1)

    def test_stop_conditions(self):
        for change in ({'done':True},{'ended':True},{'end_time':time.time()},
                       {'recovered':True},{'polled':True},{'attempts':6},{'live_fetches':12},
                       {'live_updated_at':time.time()-11},{'created':time.time()-31}):
            with self.subTest(change=change):
                p,st=self.processor();st.update(change);p._schedule_live_jobs()
                self.assertTrue(p.queue.empty())

    def test_no_new_sampling_for_disabled_or_nonopted_camera_or_disconnected(self):
        for profile in ({'enabled':False,'fast_recognition':True,'high_resolution':True},
                        {'fast_recognition':False,'high_resolution':True},
                        {'fast_recognition':True,'high_resolution':False}):
            p,st=self.processor();p.camera_profiles=SimpleNamespace(get=lambda _:profile,all=lambda:{'front':profile})
            p._schedule_live_jobs();self.assertTrue(p.queue.empty())
        p,st=self.processor();p.access1.transport=None;p._schedule_live_jobs()
        self.assertTrue(p.queue.empty())

    def test_clip_job_prevents_same_event_live_requeue(self):
        p,st=self.processor();p._enqueue_job('e','clip');p._schedule_live_jobs()
        self.assertTrue(p.queue.empty())

    def test_live_request_uses_event_not_recordings_or_archival_snapshot(self):
        p,st=self.processor();p._process('e')
        p.frigate.live_event_frame.assert_called_once_with('e')
        p.frigate.recording_frame.assert_not_called()
        p.frigate.snapshot.assert_not_called()
        self.assertEqual(st['attempts'],0)

    def test_invalid_capture_times_do_not_fetch(self):
        for timestamp in (None, '100', True, float('nan'), float('inf'), time.time()+3, time.time()-11):
            p,st=self.processor();st['live_snapshot_ts']=timestamp;p._process('e')
            p.frigate.live_event_frame.assert_not_called()

    def test_no_capture_before_event_start(self):
        p,st=self.processor();st['start_time']=time.time();p._process('e')
        p.frigate.live_event_frame.assert_not_called()

    def test_live_budget_also_applies_to_mqtt_jobs(self):
        p,st=self.processor();st['live_fetches']=12;p._process('e')
        p.frigate.live_event_frame.assert_not_called()

    def test_duplicate_frames_do_not_build_liveness(self):
        p,st=self.processor()
        a=np.zeros((100,100,3),dtype=np.uint8);b=np.ones((100,100,3),dtype=np.uint8)
        p.frigate.live_event_frame.side_effect=[a,b,a]
        for i in range(3):
            st['live_snapshot_ts']=time.time()-3+i;p._process('e')
        self.assertEqual(st['attempts'],2)
        self.assertEqual(p.engine.faces.call_count,2)

    def test_slow_fetch_is_rejected_before_inference(self):
        p,st=self.processor();now=time.time()
        def fetch(*args,**kwargs):
            clock.return_value=now+12
            return np.zeros((100,100,3),dtype=np.uint8)
        p.frigate.live_event_frame.side_effect=fetch
        with patch('app.mqtt_listener.time.time',return_value=now) as clock:p._process('e')
        p.engine.faces.assert_not_called()

    def test_ended_event_preserves_archival_fallback(self):
        p,st=self.processor();st['ended']=True;st['end_time']=time.time()
        p._process('e');p.frigate.snapshot.assert_called_once_with('e',crop=True)
        self.assertEqual(p.frigate.recording_frame.call_args.kwargs,{})

    def test_optional_timeout_does_not_change_enrollment_default_or_tls(self):
        api=FrigateAPI('https://fixture.invalid',timeout=6,verify_tls=True)
        api.request=Mock(return_value=SimpleNamespace(status_code=404))
        api.recording_frame('door',10);self.assertEqual(api.request.call_args.kwargs['timeout'],24)
        api.recording_frame('door',10,timeout=2);self.assertEqual(api.request.call_args.kwargs['timeout'],2)
        self.assertTrue(api.session.verify)

    def test_inference_exception_allows_same_frame_retry(self):
        p,st=self.processor()
        p.frigate.live_event_frame.return_value=np.zeros((100,100,3),dtype=np.uint8)
        p.engine.faces.side_effect=[RuntimeError('temporary inference failure'),[]]
        with self.assertRaises(RuntimeError):p._process('e')
        self.assertNotIn('live_snapshot_consumed_ts',st)
        p._process('e')
        self.assertEqual(p.frigate.live_event_frame.call_count,2)
        self.assertEqual(st['live_snapshot_consumed_ts'],st['live_snapshot_ts'])

    def test_same_capture_timestamp_never_counts_twice(self):
        p,st=self.processor()
        p.frigate.live_event_frame.return_value=np.zeros((100,100,3),dtype=np.uint8)
        p._process('e');p._process('e')
        self.assertEqual(p.frigate.live_event_frame.call_count,1)
        self.assertEqual(st['attempts'],1)

    def test_end_during_fetch_cannot_produce_live_evidence(self):
        p,st=self.processor()
        def fetch(*args):
            st['ended']=True
            return np.zeros((100,100,3),dtype=np.uint8)
        p.frigate.live_event_frame.side_effect=fetch;p._process('e')
        self.assertIsNone(st['access1_evidence_ts'])
        p.engine.faces.assert_not_called()

    def test_mqtt_update_during_fetch_does_not_refresh_image_age(self):
        p,st=self.processor();timestamp=st['live_snapshot_ts']
        def fetch(*args):
            st['live_snapshot_ts']=time.time()
            return np.zeros((100,100,3),dtype=np.uint8)
        p.frigate.live_event_frame.side_effect=fetch;p._process('e')
        self.assertEqual(st['access1_evidence_ts'],timestamp)

    def test_actual_mqtt_metadata_uses_snapshot_time_not_arrival_or_current_frame(self):
        p,st=self.processor();p._ensure_discovery=Mock()
        timestamp=time.time()-4
        after={'id':'e','camera':'front','label':'person','frame_time':time.time(),
               'snapshot':{'frame_time':timestamp},'has_snapshot':True}
        msg=SimpleNamespace(topic='frigate/events',payload=json.dumps({'type':'update','after':after}),retain=False)
        p._on_message(None,None,msg)
        self.assertEqual(st['live_snapshot_ts'],timestamp)
        msg.retain=True;p._on_message(None,None,msg)
        self.assertIsNone(st['live_snapshot_ts'])
        msg.retain=False;after.pop('snapshot')
        msg.payload=json.dumps({'type':'update','after':after});p._on_message(None,None,msg)
        self.assertIsNone(st['live_snapshot_ts'])

class LiveFrameHttpTests(unittest.TestCase):
    def api(self, status=200, cache='no-store', data=None, age='0'):
        api=FrigateAPI('https://fixture.invalid',verify_tls=True)
        if data is None:
            data=cv2.imencode('.jpg',np.zeros((64,80,3),dtype=np.uint8))[1].tobytes()
        response=Mock(status_code=status,content=data,headers={'Cache-Control':cache,'Age':age})
        api.request=Mock(return_value=response)
        return api,response

    def test_active_full_frame_and_bounded_request(self):
        api,r=self.api();img=api.live_event_frame('e/id')
        self.assertEqual(img.shape,(64,80,3))
        args,kwargs=api.request.call_args
        self.assertEqual(args,('GET','/api/events/e%2Fid/snapshot.jpg'))
        self.assertEqual(kwargs['params'],{'crop':0,'timestamp':0,'bbox':0,'quality':100})
        self.assertEqual(kwargs['timeout'],2.0)
        self.assertTrue(api.session.verify);r.close.assert_called_once()

    def test_completed_cached_missing_invalid_http_responses_fail_closed(self):
        for options in ({'cache':'private, max-age=31536000'},{'cache':''},
                        {'age':'1'},{'status':401},{'status':404},{'status':500},
                        {'data':b''},{'data':b'<html>not an image</html>'}):
            with self.subTest(options=options):
                api,r=self.api(**options)
                with self.assertLogs('faceid.frigate',level='INFO'):
                    self.assertIsNone(api.live_event_frame('e'))
                r.close.assert_called_once()

    def test_timeout_diagnostic_does_not_leak_credentials(self):
        api,r=self.api();api.request.side_effect=requests.Timeout('secret-password')
        with self.assertLogs('faceid.frigate',level='INFO') as logs:
            self.assertIsNone(api.live_event_frame('e'))
        self.assertIn('Timeout',str(logs.output));self.assertNotIn('secret-password',str(logs.output))

class ConnectorDiagnosticTests(unittest.TestCase):
    setUp = baseline_tests.Access1Tests.setUp
    event = baseline_tests.Access1Tests.event
    ready = baseline_tests.Access1Tests.ready
    def test_specific_block_reasons_preserve_denial(self):
        self.ready()
        for changes,reason in [({'evidence_source':'clip:3'},'evidence_not_live'),
            ({'evidence_source':'snapshot'},'evidence_not_live'),
            ({'event_ended':True},'event_already_ended'),
            ({'evidence_ts':time.time()-11},'evidence_expired_or_missing'),
            ({'evidence_ts':time.time()+3},'evidence_expired_or_missing'),
            ({'liveness_confirmed':False},'liveness_unconfirmed')]:
            event=self.event();event.update(changes)
            self.assertEqual(self.integration.present(event)['reason'],reason)
            self.assertEqual(self.integration.status()['last_submission']['face_event_id'],'frigate-event')
        self.integration.transport.call.assert_not_called()

    def test_fresh_confirmed_live_frame_still_submits_once(self):
        self.ready();event=self.event()
        self.assertEqual(self.integration.present(event)['state'],'accepted')
        self.assertEqual(self.integration.present(event)['state'],'duplicate')
        self.integration.transport.call.assert_called_once()

    def test_timestamped_active_snapshot_preserves_all_connector_safeguards(self):
        self.ready()
        for change in ({'evidence_ts':time.time()-11}, {'evidence_ts':time.time()+3},
                       {'event_ended':True}, {'liveness_confirmed':False}, {'event_id':''}):
            event=self.event();event['evidence_source']='secure_live_snapshot';event.update(change)
            self.assertEqual(self.integration.present(event)['state'],'blocked')
        self.integration.transport.call.assert_not_called()
        event=self.event();event['evidence_source']='secure_live_snapshot'
        self.assertEqual(self.integration.present(event)['state'],'accepted')
        self.assertEqual(self.integration.present(event)['state'],'duplicate')
        self.integration.transport.call.assert_called_once()
