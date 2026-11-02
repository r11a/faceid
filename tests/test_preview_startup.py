from types import SimpleNamespace as NS
from unittest import TestCase
from unittest.mock import Mock,patch
import numpy as np
from app import preview_startup as module
from app.crop_diagnostics import PreviewCache
from test_decode_diagnostics import processor


class Clock:
    value=0.
    def monotonic(self):return self.value
    def sleep(self,n):self.value+=max(n,.001)


class StartupTests(TestCase):
    def run_preview(self,images,missing=False,freeze=False,safety_after=None,decoder_error=False):
        clock=Clock();p=processor();p.engine.detect_faces=Mock(side_effect=AssertionError('no inference'))
        def blockers(*args):return ['safety_changed'] if safety_after and clock.value>=safety_after else []
        class Reader:
            def __init__(self,*args,**kwargs):self.stopped=False;self.seq=0;self.thread=NS(start=lambda:None)
            def take(self):
                if self.stopped:return None,{'decoder_stopped':True,'decoded_frames':self.seq,**({'error':'decoder_failed'} if decoder_error else {})}
                self.seq=1 if freeze else self.seq+1
                frame=images(clock.value)
                return (self.seq,clock.value,0 if missing else self.seq*40,'pts_missing' if missing else None,frame),{}
            def stop(self):self.stopped=True
        seen=[]
        with patch.object(module,'time',clock):
            result=module.inspect_startup(p,'Enterance','rtsp://frigate/a',[.2,.2,.8,.8],
                lambda images:seen.append(set(images)),Reader,blockers)
        p.engine.detect_faces.assert_not_called()
        return result,seen
    @staticmethod
    def gray():return np.full((120,160,3),128,np.uint8)
    @staticmethod
    def detailed():return np.repeat((((np.indices((120,160)).sum(axis=0)//8)%2)*255).astype(np.uint8)[...,None],3,axis=2)
    def test_gray_startup_then_detail_is_observed_over_full_window(self):
        result,seen=self.run_preview(lambda t:self.gray() if t<3 else self.detailed())
        self.assertEqual(result['state'],'preview_detail_recovered')
        self.assertGreaterEqual(result['elapsed_ms'],11000)
        self.assertGreater(len(result['samples']),15);self.assertLessEqual(len(result['samples']),24)
        self.assertGreaterEqual(result['first_structured_ms'],3000)
        self.assertIn('startup',seen[-1]);self.assertTrue(result['decoder']['decoder_stopped'])
        self.assertFalse(result['admission_eligible']);self.assertFalse(result['freshness_proven'])
    def test_persistent_gray_never_reports_ready(self):
        result,_=self.run_preview(lambda _:self.gray())
        self.assertEqual(result['state'],'preview_low_detail')
        self.assertEqual(result['low_detail_samples'],len(result['samples']))
    def test_missing_timestamps_are_separate_from_visual_detail(self):
        result,_=self.run_preview(lambda _:self.detailed(),missing=True)
        self.assertEqual(result['state'],'preview_detail_observed')
        self.assertEqual(result['timestamp_issue_samples'],len(result['samples']))
        self.assertFalse(result['freshness_proven'])
    def test_one_detailed_frame_cannot_establish_stability(self):
        result,_=self.run_preview(lambda _:self.detailed(),freeze=True)
        self.assertEqual(result['state'],'preview_inconclusive');self.assertEqual(len(result['samples']),1)
    def test_detail_that_disappears_does_not_report_recovery(self):
        result,_=self.run_preview(lambda t:self.detailed() if t<6 else self.gray())
        self.assertEqual(result['state'],'preview_low_detail')
    def test_decoder_failure_prevents_a_successful_observation_result(self):
        result,_=self.run_preview(lambda _:self.detailed(),decoder_error=True)
        self.assertEqual(result['state'],'preview_inconclusive')
        self.assertEqual(result['visual_state_before_decoder_check'],'preview_detail_observed')
    def test_runtime_safety_abort_stops_decoder(self):
        result,_=self.run_preview(lambda _:self.gray(),safety_after=2)
        self.assertEqual(result['state'],'preview_blocked');self.assertTrue(result['decoder']['decoder_stopped'])
    def test_contact_sheet_bounded_four_views(self):
        tiles={i:(self.detailed(),'test') for i in range(4)}
        self.assertEqual(module.contact_sheet(tiles).shape,(528,640,3))
    def test_timeline_uses_same_owner_expiry_and_clear(self):
        cache=PreviewCache()
        try:
            cache.put('owner',{'startup':self.detailed()})
            self.assertIn('startup',cache.status('owner')['kinds'])
            self.assertEqual(cache.status('other')['kinds'],[])
            self.assertTrue(cache.get('owner','startup').startswith(b'\xff\xd8'))
            cache.clear();self.assertFalse(cache.items)
        finally:cache.clear()
