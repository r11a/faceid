"""Offline admission-contract tests; no camera/network/relay is contacted."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from app.camera_profiles import CameraProfiles
from app.mqtt_listener import EventProcessor
from app.liveness import LivenessDetector
from app.access1 import Access1Integration
from app.live_frames import LiveFrameReader, LiveFramePool
from test_access1 import Audit, Gallery


class StreamTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.profiles=CameraProfiles(self.root/'profiles.json')
        self.profiles.update('front',min_face_px=24,role='entry',observation_source='frigate_restream',trust_local_video_network=True)
        self.reader=SimpleNamespace(generation=1,record=None,frame_record=lambda:self.reader.record)
        self.frigate=SimpleNamespace(live_frames=SimpleNamespace(acquire_observation=Mock(return_value=self.reader),release_observation=Mock()),
            _live_url=lambda _: 'rtsp://frigate:8554/front',association_frame=Mock(return_value=np.zeros((100,100,3),np.uint8)))
        self.faces=[]
        self.engine=SimpleNamespace(detect_faces=lambda image:[self.face(0)] if image[0,0,0]==0 else self.faces,
                                    embed_face=lambda image,face:face)
        self.gallery=Gallery(self.root)
        self.gallery.match_candidates=Mock(return_value=[('alice','Alice',.9),('other','Other',.1)])
        self.gallery.match_ignored_detail=Mock(return_value=(0,None,None))
        self.p=EventProcessor({'faceid':{}},self.engine,self.gallery,self.frigate)
        self.p.camera_profiles=self.profiles
        self.p.liveness=LivenessDetector()
        self.p.liveness.analyze=Mock(return_value={'state':'live','live':True,'score':.9,'face_px':80})
        self.p._publish_recognition=Mock();self.p._publish_notification=Mock();self.p._flush_samples=Mock()
        self.integration=Access1Integration(self.root,Audit(self.root/'audit.db'),gallery=self.gallery,processor=self.p)
        self.integration.config['cameras']['front']='camera-1'
        self.integration.transport=Mock()
        self.integration.transport.call.return_value={'status':'relay_activated','reason':'output_on','execution_confirmed':True}
        self.p.access1=self.integration
        self.m=self.p.stream_observations
        self.m._quality=lambda *args:SimpleNamespace(usable=True,score=.9,to_dict=lambda:{'score':.9,'sharpness':100})
        self.addCleanup(self.m.executor.shutdown,wait=True)

    def face(self,n,bbox=None,other=False):
        vector=np.array([n/100,1.] if other else [1.,n/100],np.float32)
        return SimpleNamespace(bbox=np.array(bbox or [0,0,80,80],np.float32),normed_embedding=vector/np.linalg.norm(vector),det_score=.99)

    def trigger(self,eid='event-1',kind='new',**changes):
        after={'id':eid,'camera':'front','label':'person','start_time':time.time(),**changes}
        self.p._on_message(None,None,SimpleNamespace(topic='frigate/events',payload=json.dumps({'type':kind,'after':after}).encode(),retain=False))
        return self.m.sessions.get(eid)

    def frame(self,n,faces=None):
        self.faces=faces if faces is not None else [self.face(n)]
        self.reader.record={'sequence':n,'generation':self.reader.generation,'arrived':time.monotonic(),
                            'pts_ms':1000+n*250,'image':np.full((100,100,3),n,np.uint8)}
        self.m._step('front')

    def confirmed(self):
        s=self.trigger()
        for n in (1,2,3):self.frame(n)
        return s

    def test_integrated_mqtt_anchor_three_samples_and_access1_result(self):
        s=self.trigger()
        for n in (1,2):
            self.frame(n);self.assertTrue(self.integration._queue.empty())
        self.frame(3)
        self.assertEqual(s['st']['stream_samples'],3)
        self.assertTrue(s['st']['done'])
        event=self.integration._queue.get_nowait()
        self.assertEqual(event['evidence_source'],'bounded_local_stream')
        result=self.integration.present(event)
        self.assertEqual(result['state'],'relay_activated')
        payload=self.integration.transport.call.call_args.args[2]
        self.assertEqual(payload['liveness']['confirmed_frames'],3)
        self.assertNotIn('stream_proof',payload)
        self.assertFalse(self.m.sessions)
        self.frigate.live_frames.release_observation.assert_called_once_with('front')

    def test_bystander_can_be_present_without_receiving_event(self):
        s=self.trigger()
        for n in (1,2,3):self.frame(n,[self.face(n),self.face(n,[120,0,200,80],True)])
        self.assertTrue(s['st']['done'])

    def test_face_detector_order_cannot_change_event_subject(self):
        s=self.trigger()
        for n in (1,2,3):
            faces=[self.face(n),self.face(n,[120,0,200,80],True)]
            self.frame(n,faces if n%2 else faces[::-1])
        self.assertTrue(s['st']['done'])
        self.assertEqual(s['st']['stream_samples'],3)

    def test_subject_leaves_bystander_cannot_complete_history(self):
        s=self.trigger();self.frame(1);self.frame(2)
        self.frame(3,[self.face(3,[120,0,200,80],True)])
        self.assertEqual(s['st']['stream_samples'],0)
        self.assertTrue(self.integration._queue.empty())

    def test_competing_events_do_not_both_bind_one_face(self):
        a=self.trigger('a');b=self.trigger('b')
        for n in (1,2,3):self.frame(n)
        self.assertEqual(a['st']['stream_samples'],0);self.assertEqual(b['st']['stream_samples'],0)
        self.assertTrue(self.integration._queue.empty())

    def test_overcrowding_rejects_instead_of_selecting_four(self):
        s=self.trigger();self.frame(1,[self.face(i) for i in range(5)])
        self.assertEqual(s['reason'],'overcrowded_frame')

    def test_ambiguous_anchor_fails_after_two_attempts(self):
        s=self.trigger();self.engine.detect_faces=lambda _: [self.face(0),self.face(1)]
        for n in range(1,4):self.frame(n)
        self.assertNotIn('event-1',self.m.sessions)
        self.assertEqual(self.frigate.association_frame.call_count,2)
        self.assertTrue(self.integration._queue.empty())

    def test_reconnect_clears_evidence_and_keeps_deadline(self):
        s=self.trigger();deadline=s['deadline'];self.frame(1);self.frame(2)
        self.reader.generation=2;self.frame(3)
        self.assertEqual(s['st']['stream_samples'],1)
        self.assertEqual(s['deadline'],deadline);self.assertTrue(self.integration._queue.empty())

    def test_end_cancels_pending_delivery(self):
        self.confirmed();event=self.integration._queue.get_nowait();self.trigger(kind='end',end_time=time.time())
        self.assertEqual(self.integration.present(event)['state'],'blocked')
        self.integration.transport.call.assert_not_called()

    def test_profile_change_cancels_delivery(self):
        self.confirmed();event=self.integration._queue.get_nowait();self.profiles.set_enabled('front',False)
        self.assertEqual(self.integration.present(event)['state'],'blocked');self.integration.transport.call.assert_not_called()

    def test_evidence_expiry_at_delivery(self):
        s=self.confirmed();event=self.integration._queue.get_nowait();s['st']['stream_deadline']=time.monotonic()-1
        self.assertEqual(self.integration.present(event)['reason'],'evidence_expired');self.integration.transport.call.assert_not_called()

    def test_disconnected_reader_blocks_queued_delivery(self):
        self.confirmed();event=self.integration._queue.get_nowait();self.reader.record=None
        self.assertEqual(self.integration.present(event)['reason'],'stream_not_ready')
        self.integration.transport.call.assert_not_called()

    def test_clock_jump_blocks_delivery(self):
        s=self.confirmed();event=self.integration._queue.get_nowait();s['wall_started']-=3
        self.assertEqual(self.integration.present(event)['reason'],'clock_changed')

    def test_repeated_event_does_not_extend_deadline(self):
        s=self.trigger();deadline=s['deadline'];self.trigger(kind='update')
        self.assertEqual(s['deadline'],deadline)

    def test_duplicate_pixels_do_not_confirm(self):
        s=self.trigger();self.frame(1);self.frame(1);self.frame(1)
        self.assertEqual(s['st']['stream_samples'],1);self.assertTrue(self.integration._queue.empty())

    def test_duplicate_embedding_does_not_confirm(self):
        s=self.trigger()
        for n in (1,2,3):self.frame(n,[self.face(1)])
        self.assertEqual(s['st']['stream_samples'],1);self.assertTrue(self.integration._queue.empty())

    def test_wrong_identity_resets(self):
        s=self.trigger();self.frame(1);self.frame(2)
        self.gallery.match_candidates.return_value=[('other','Other',.9)]
        self.frame(3)
        self.assertEqual(s['st']['stream_samples'],1);self.assertTrue(self.integration._queue.empty())

    def test_crossing_implausible_motion_starts_fresh(self):
        s=self.trigger();self.frame(1);self.frame(2);self.frame(3,[self.face(3,[1000,0,1080,80])])
        self.assertEqual(s['st']['stream_samples'],1)

    def test_spoof_resets(self):
        s=self.trigger();self.frame(1);self.frame(2)
        self.p.liveness.analyze.return_value={'state':'spoof','live':False,'score':.1}
        self.frame(3);self.assertEqual(s['st']['stream_samples'],0)

    def test_no_trust_does_not_start_reader(self):
        self.profiles.update('front',min_face_px=24,role='entry',trust_local_video_network=False)
        self.assertIsNone(self.trigger());self.frigate.live_frames.acquire_observation.assert_not_called()

    def test_four_sessions_limit(self):
        for n in range(5):self.trigger('e'+str(n))
        self.assertEqual(len(self.m.sessions),4)
        self.assertEqual(self.m.completed[-1]['reason'],'session_capacity')

    def test_stricter_config_retained(self):
        self.p.liveness.required_frames=4;s=self.trigger()
        for n in (1,2,3):self.frame(n)
        self.assertFalse(s['st']['done']);self.frame(4);self.assertTrue(s['st']['done'])

    def test_stale_arrival_not_used(self):
        s=self.trigger();self.frame(1);self.reader.record['arrived']-=2
        self.reader.record['sequence']=2;self.m._step('front')
        self.assertEqual(s['st']['stream_samples'],1)

    def test_diagnostics_do_not_cancel_active_session(self):
        from app.decode_diagnostics import blockers
        s=self.trigger();self.assertEqual(blockers(self.p,'front'),[])
        self.frame(1);self.assertEqual(s['st']['stream_samples'],1)

    def test_overlap_resets_affected_evidence(self):
        s=self.trigger();self.frame(1);self.frame(2)
        self.frame(3,[self.face(3),self.face(3,[40,0,120,80],True)])
        self.assertEqual(s['st']['stream_samples'],0)
        self.assertEqual(s['reason'],'overlap_or_crossing')

    def test_reconnect_after_confirmation_blocks_delivery(self):
        self.confirmed();event=self.integration._queue.get_nowait();self.reader.generation+=1
        self.assertEqual(self.integration.present(event)['reason'],'stream_association_changed')
        self.integration.transport.call.assert_not_called()

    def test_uncertain_post_only_polls_no_repost(self):
        self.confirmed();event=self.integration._queue.get_nowait()
        self.integration.transport.call.side_effect=[TimeoutError(),{'status':'relay_activated'}]
        self.assertEqual(self.integration.present(event)['state'],'relay_activated')
        methods=[call.args[0] for call in self.integration.transport.call.call_args_list]
        self.assertEqual(methods,['POST','GET'])

    def test_repeat_delivery_cannot_activate_twice(self):
        self.confirmed();event=self.integration._queue.get_nowait()
        self.integration.present(event);self.integration.present(event)
        self.assertEqual(self.integration.transport.call.call_count,1)

    def test_mapping_change_blocks_delivery(self):
        self.confirmed();event=self.integration._queue.get_nowait()
        self.integration.config['cameras']['front']='changed'
        self.assertEqual(self.integration.present(event)['reason'],'refresh_camera_directory')
        self.integration.transport.call.assert_not_called()

    def test_first_qualifying_frame_deadline_never_extended(self):
        s=self.trigger();self.frame(1);deadline=s['st']['stream_deadline']
        self.frame(2);self.frame(3)
        self.assertEqual(s['st']['stream_deadline'],deadline)

    def test_mqtt_disconnect_clears_all_evidence(self):
        s=self.trigger();self.frame(1)
        self.p._on_message(None,None,SimpleNamespace(topic='frigate/available',payload=b'offline',retain=False))
        self.assertFalse(self.m.sessions)
        self.assertEqual(self.m.completed[-1]['reason'],'frigate_offline')

    def test_preview_private_and_excluded_from_report(self):
        s=self.trigger();self.frame(1)
        self.assertIsInstance(self.m.preview(s['eid'],'anchor'),bytes)
        self.assertNotIn('image',json.dumps(self.m.report()))
        self.m.previews[s['eid']]['updated']-=181
        self.assertIsNone(self.m.preview(s['eid'],'anchor'))

    def test_direct_mode_uses_only_saved_validated_source(self):
        url='rtsp://test:test-secret@10.0.0.100:554/camera'
        self.profiles.update('front',min_face_px=24,role='entry',observation_source='direct_rtsp',direct_rtsp_url=url)
        self.trigger()
        self.frigate.live_frames.acquire_observation.assert_called_once_with('front',url)
        self.assertNotIn('test-secret',json.dumps(self.m.report()))

    def test_database_delay_cannot_extend_evidence(self):
        from contextlib import contextmanager
        s=self.confirmed();event=self.integration._queue.get_nowait()
        original=self.integration.audit._connection
        @contextmanager
        def delayed():
            with original() as connection:
                yield connection
            s['st']['stream_deadline']=time.monotonic()-1
        with patch.object(self.integration.audit,'_connection',delayed):
            self.assertEqual(self.integration.present(event)['state'],'blocked')
        self.integration.transport.call.assert_not_called()

    def test_direct_secret_masked_retained_and_explicitly_cleared(self):
        secret='rtsp://test:private-secret@10.0.0.100:554/camera'
        self.profiles.update('front',min_face_px=24,role='entry',observation_source='direct_rtsp',direct_rtsp_url=secret)
        self.assertNotIn(secret,json.dumps(self.profiles.all()))
        self.profiles.update('front',min_face_px=24,role='entry')
        self.assertEqual(self.profiles.observation_config('front')[1],secret)
        self.profiles.update('front',min_face_px=24,role='entry',observation_source='snapshots',clear_direct_rtsp=True)
        self.assertEqual(self.profiles.observation_config('front')[1],'')

    def test_legacy_default(self):
        self.assertEqual(self.profiles.get('old')['observation_source'],'snapshots')
        self.assertFalse(self.profiles.get('old')['trust_local_video_network'])

    def client(self):
        from fastapi.testclient import TestClient
        from app.webui import build_app
        processor=SimpleNamespace(frigate=self.frigate,cameras={'front'},audit=None,camera_profiles=self.profiles,
                                  min_face_px=24,min_face_quality=.3,engine=self.engine)
        return TestClient(build_app({'faceid':{'auth':{}}},self.engine,self.gallery,processor,self.root,self.root))

    def test_unchanged_source_fields_preserve_existing_settings_save(self):
        response=self.client().post('/api/cameras/legacy/profile',json={'min_face_px':24,'role':'entry',
            'observation_source':'snapshots','trust_local_video_network':False})
        self.assertEqual(response.status_code,200,response.text)

    def test_camera_api_requires_verified_admin_for_stream_settings(self):
        client=self.client()
        response=client.post('/api/cameras/front/profile',json={'min_face_px':24,'role':'entry','observation_source':'direct_rtsp','direct_rtsp_url':'rtsp://user:private-secret@10.0.0.100/x'})
        self.assertEqual(response.status_code,403)
        self.assertNotIn('private-secret',response.text)

    def test_camera_api_validation_never_echoes_credentials(self):
        response=self.client().post('/api/cameras/front/profile',json={'min_face_px':'bad','role':'entry','direct_rtsp_url':'rtsp://user:private-secret@10.0.0.100/x'})
        self.assertEqual(response.status_code,422)
        self.assertNotIn('private-secret',response.text)

    def test_camera_api_masked_success(self):
        from unittest.mock import AsyncMock
        with patch('app.access1_admin.require_pairing_admin',new=AsyncMock(return_value='admin')):
            response=self.client().post('/api/cameras/front/profile',json={'min_face_px':24,'role':'entry','observation_source':'direct_rtsp','direct_rtsp_url':'rtsp://user:private-secret@10.0.0.100/x'})
        self.assertEqual(response.status_code,200,response.text)
        self.assertNotIn('private-secret',response.text)
        self.assertTrue(response.json()['profile']['direct_rtsp_configured'])

    def test_unsafe_urls_rejected_without_echo(self):
        for url in ('http://secret@10.0.0.1/x','rtsp://secret@127.0.0.1/x','rtsp://secret@8.8.8.8/x','rtsp://secret@camera.local/x'):
            with self.assertRaises(ValueError) as caught:self.profiles.update('front',min_face_px=24,role='entry',direct_rtsp_url=url)
            self.assertNotIn('secret',str(caught.exception))


class ReaderTests(unittest.TestCase):
    def test_observation_lease_survives_regular_sync_and_releases(self):
        with patch.object(LiveFrameReader,'start'):
            pool=LiveFramePool();r=pool.acquire_observation('front','rtsp://one')
            pool.sync({});self.assertIs(pool.readers['front'],r)
            pool.release_observation('front');self.assertNotIn('front',pool.readers)

    def test_two_reader_cap_includes_retiring_threads(self):
        with patch.object(LiveFrameReader,'start'):
            pool=LiveFramePool();a=pool.acquire_observation('a','a');b=pool.acquire_observation('b','b')
            a.thread=SimpleNamespace(is_alive=lambda:True)
            pool.release_observation('a')
            self.assertIsNone(pool.acquire_observation('c','c'))

    def test_repeated_and_backward_pts_rejected(self):
        r=LiveFrameReader('private')
        self.assertTrue(r.accept_position(1000,10,0))
        self.assertFalse(r.accept_position(1000,10.1,0));self.assertFalse(r.accept_position(900,10.2,0))
        self.assertFalse(r.accept_position(1100,11,0))

    def test_stale_frame_record_not_returned(self):
        r=LiveFrameReader('private');r.state='streaming';r.decoded={'arrived':time.monotonic()-2}
        self.assertIsNone(r.frame_record())


if __name__=='__main__':unittest.main()
