import time
import unittest
import warnings
from types import SimpleNamespace
from unittest.mock import Mock
import numpy as np
from app.mqtt_listener import EventProcessor
from app.liveness import LivenessDetector
from urllib3.exceptions import InsecureRequestWarning

class LiveConsensusTests(unittest.TestCase):
    def setup_processor(self):
        p=EventProcessor({'faceid':{}},SimpleNamespace(),Mock(),Mock())
        p.camera_profiles=SimpleNamespace(get=lambda _: {'liveness_mode':'required'})
        p.liveness=LivenessDetector()
        p.liveness.analyze=Mock(return_value={'state':'live','live':True,'face_px':80,'score':.9})
        p.gallery.match_candidates.return_value=[('d','David',.8),('other','Other',.1)]
        p.gallery.match_ignored_detail.return_value=(0,None,None)
        p.access1=SimpleNamespace(enqueue=Mock())
        p._publish_recognition=Mock()
        st=p._new_event_state('e','front');p.events['e']=st
        return p,st

    def frame(self,p,st,n,source='secure_live_snapshot',age=1):
        st['attempts']+=1;st['access1_evidence_ts']=time.time()-age
        face=SimpleNamespace(bbox=np.array([0,0,80,80]),det_score=.99,normed_embedding=np.array([1.,n/100.],dtype=np.float32))
        p._process_face('e',st,np.zeros((100,100,3),dtype=np.uint8),face,source=source)

    def test_three_distinct_live_frames_complete_both_checks_not_four(self):
        p,st=self.setup_processor()
        for n in (1,2):
            self.frame(p,st,n)
            p.access1.enqueue.assert_not_called();p._publish_recognition.assert_not_called()
            self.assertFalse(st['done'])
        self.frame(p,st,3)
        p.access1.enqueue.assert_called_once()
        payload=p.access1.enqueue.call_args.args[0]
        self.assertTrue(payload['liveness_confirmed'])
        self.assertEqual(payload['liveness_frames'],3)
        self.assertEqual(payload['evidence_source'],'secure_live_snapshot')
        self.assertGreaterEqual(st['final_decision'].confirmations,2)

    def test_failed_liveness_never_contributes_identity(self):
        for result in ({'state':'insufficient','live':None,'face_px':30},
                       {'state':'spoof','live':False,'face_px':80},
                       {'state':'unavailable','live':None,'face_px':80}):
            p,st=self.setup_processor();p.liveness.analyze.return_value=result
            for n in range(3):self.frame(p,st,n)
            p.gallery.match_candidates.assert_not_called();p.access1.enqueue.assert_not_called()

    def test_stale_or_ended_pending_frames_not_precollected(self):
        for age,ended in ((11,False),(1,True)):
            p,st=self.setup_processor();st['ended']=ended
            self.frame(p,st,1,age=age)
            p.gallery.match_candidates.assert_not_called();p.access1.enqueue.assert_not_called()

    def test_clip_and_ordinary_snapshot_policy_unchanged(self):
        for source in ('clip:1','snapshot','secure_live_hires'):
            p,st=self.setup_processor()
            for n in (1,2):self.frame(p,st,n,source)
            p.gallery.match_candidates.assert_not_called()
            self.frame(p,st,3,source)
            p.access1.enqueue.assert_not_called()

    def test_identity_disagreement_does_not_bypass_confirmation(self):
        p,st=self.setup_processor()
        for n in range(3):
            p.gallery.match_candidates.return_value=[(str(n),str(n),.8)]
            self.frame(p,st,n)
        p.access1.enqueue.assert_not_called()

    def test_duplicate_identity_embedding_does_not_confirm(self):
        p,st=self.setup_processor()
        for _ in range(3):self.frame(p,st,1)
        p.access1.enqueue.assert_not_called()

    def test_spoof_after_two_live_frames_cannot_publish(self):
        p,st=self.setup_processor()
        for n in (1,2):self.frame(p,st,n)
        p.liveness.analyze.return_value={'state':'spoof','live':False,'face_px':80}
        self.frame(p,st,3)
        p.access1.enqueue.assert_not_called();p._publish_recognition.assert_not_called()

    def test_repeated_tls_warning_remains_visible_once(self):
        with warnings.catch_warnings(record=True) as seen:
            warnings.filterwarnings('default',category=InsecureRequestWarning,module=r'urllib3\.connectionpool')
            registry={}
            for _ in range(4):
                warnings.warn_explicit('Unverified HTTPS request',InsecureRequestWarning,'connectionpool.py',1129,
                                       module='urllib3.connectionpool',registry=registry)
            self.assertEqual(len(seen),1)

if __name__=='__main__':unittest.main()
