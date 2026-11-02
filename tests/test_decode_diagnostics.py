import ast
import json
import tempfile
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
import numpy as np
from app import decode_diagnostics as module


class Reader:
    def __init__(self,url,seconds=45):
        self.seq=0;self.stopped=False;self.thread=NS(start=lambda:None)
    def take(self):
        if self.stopped:return None,{'decoder_stopped':True,'decoded_frames':self.seq}
        self.seq+=1
        return (self.seq,module.time.monotonic(),self.seq*40,None,np.full((100,100,3),self.seq,dtype=np.uint8)),{'decoded_frames':self.seq}
    def stop(self):self.stopped=True


def processor(live=True):
    face=NS(bbox=np.array([10,10,90,90]),det_score=.99,normed_embedding=np.array([1.,0.]))
    def embed(image,face,timings):
        face.normed_embedding=np.array([1.,float(image[0,0,0])/255],dtype=np.float32)
        timings.update(embedding_compute=.001,embedding_scheduler_wait=0.)
    engine=NS(health=lambda:{'backend':'CPU'},background_work=nullcontext,model_work=nullcontext,
        detect_faces=lambda frame:[face],embed_face=embed)
    return NS(access1=NS(transport=None,options={'enabled':False}),camera_enabled=lambda _:False,
        engine=engine,gallery=NS(match_candidates=lambda *a,**k:[('private','Private Name',.9)],
            match_ignored_detail=lambda _:(0.,None,None)),camera_profiles=None,
        liveness=NS(required_frames=3,threshold=.5,analyze=lambda *a:{'state':'live' if live else 'spoof','live':live,'score':.9 if live else .1},
            consensus=lambda history:{'confirmed':len(history)>=3 and all(x['live'] for x in history)}),
        min_face_px=48,min_face_quality=.35,match_thr=.5,unknown_thr=.35,ignore_thr=.5,
        match_margin=.08,ignore_margin=.12,min_confirmations=2)


class DecodeTests(unittest.TestCase):
    def test_identical_face_embedding_cannot_be_recounted(self):
        p=processor();p.engine.embed_face=lambda *a,**k:None
        r=self.run_test(p,seconds=.25)
        self.assertEqual(r['trials'],[])
        self.assertGreater(r['reasons']['duplicate_face_embedding'],0)
    def test_diagnostic_start_never_constructs_connector_or_starts_processor(self):
        from app import diagnostic_mode
        from unittest.mock import Mock
        p=NS(start=Mock());connector=Mock()
        with patch.object(diagnostic_mode,'DIAGNOSTIC_ONLY',True):
            self.assertFalse(diagnostic_mode.start_operational_services(p,connector))
        connector.assert_not_called();p.start.assert_not_called()
        self.assertIsNone(p.access1);self.assertTrue(p.diagnostic_only)

    def test_production_start_order_preserved_when_release_flag_is_off(self):
        from app import diagnostic_mode
        calls=[];p=NS(start=lambda:calls.append('processor'))
        with patch.object(diagnostic_mode,'DIAGNOSTIC_ONLY',False):
            self.assertTrue(diagnostic_mode.start_operational_services(p,lambda:calls.append('connector')))
        self.assertEqual(calls,['connector','processor'])

    def test_diagnostic_mode_preserves_saved_enabled_camera(self):
        p=processor();p.diagnostic_only=True;p.camera_enabled=lambda _:True
        self.assertEqual(module.blockers(p,'Enterance'),[])
    def test_real_decoder_reads_generated_video_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as folder:
            path=str(Path(folder)/'synthetic.avi')
            writer=module.cv2.VideoWriter(path,module.cv2.VideoWriter_fourcc(*'MJPG'),25,(160,120))
            self.assertTrue(writer.isOpened())
            for n in range(25):writer.write(np.full((120,160,3),n*8,dtype=np.uint8))
            writer.release()
            reader=module.DecodeReader(path,seconds=2)
            reader.thread.start();reader.thread.join(4)
            _,stats=reader.take();reader.stop()
            self.assertEqual(stats['decoded_frames'],25)
            self.assertEqual(stats['resolution'],[160,120])
            self.assertFalse(reader.thread.is_alive())

    def run_test(self,p,seconds=4,reader=Reader):
        with patch.object(module,'measure_face_quality',return_value=NS(usable=True,to_dict=lambda:{'score':.9})):
            return module.run_decode(p,'Enterance','rtsp://frigate:8554/Enterance',seconds=seconds,reader_factory=reader,sample_interval=.005)

    def test_three_trials_anonymous_no_admission(self):
        result=self.run_test(processor())
        self.assertEqual(len(result['trials']),3)
        self.assertEqual(result['reasons']['diagnostic_usable'],9)
        self.assertFalse(result['freshness_proven']);self.assertFalse(result['admission_eligible'])
        self.assertEqual(result['access1_submission'],'not_implemented')
        self.assertEqual(result['frames_saved'],0)
        self.assertNotIn('Private Name',str(result));self.assertNotIn('private',str(result))
        self.assertTrue(result['decoder']['decoder_stopped'])
        json.dumps(result,allow_nan=False)

    def test_operational_connector_allowed_without_calls(self):
        p=processor();p.access1.transport=object()
        from unittest.mock import Mock
        p.access1.enqueue=Mock();p.access1.present=Mock()
        p.camera_enabled=lambda _:True
        result=self.run_test(p,seconds=.1)
        self.assertNotIn('blockers',result)
        self.assertEqual(result['access1_submission'],'not_implemented')
        p.access1.enqueue.assert_not_called();p.access1.present.assert_not_called()

    def test_automatic_recognition_is_not_paused(self):
        p=processor();p.camera_enabled=lambda _:True
        self.assertEqual(module.blockers(p,'Enterance'),[])

    def test_spoof_cannot_complete_trials(self):
        r=self.run_test(processor(False),seconds=.25)
        self.assertEqual(r['trials'],[])
        self.assertGreater(r['reasons']['liveness_spoof'],0)

    def test_multiple_faces_rejected(self):
        p=processor();p.engine.detect_faces=lambda _:[object(),object()]
        r=self.run_test(p,seconds=.15)
        self.assertEqual(r['trials'],[])
        self.assertGreater(r['reasons']['multiple_faces'],0)

    def test_missing_pts_never_counts_as_usable(self):
        class BadPTS(Reader):
            def take(self):
                item,stats=super().take()
                return ((item[0],item[1],0,'pts_missing',item[4]) if item else None),stats
        r=self.run_test(processor(),seconds=.25,reader=BadPTS)
        self.assertEqual(r['trials'],[])
        self.assertGreater(r['reasons']['pts_missing'],0)
        self.assertNotIn('embedding_total',r['stage_totals_ms'])
        self.assertNotIn('detection_with_scheduler',r['stage_totals_ms'])

    def test_stale_frame_skips_all_inference(self):
        class Stale(Reader):
            def take(self):
                item,stats=super().take()
                return ((item[0],item[1]-2,*item[2:]) if item else None),stats
        r=self.run_test(processor(),seconds=.1,reader=Stale)
        self.assertGreater(r['reasons']['decoded_frame_stale'],0)
        self.assertNotIn('detection_with_scheduler',r['stage_totals_ms'])
        self.assertEqual(r['trials'],[])

    def test_nan_pts_cannot_reach_models_even_without_reader_reason(self):
        class Bad(Reader):
            def take(self):
                item,stats=super().take()
                return ((item[0],item[1],float('nan'),None,item[4]) if item else None),stats
        r=self.run_test(processor(),seconds=.1,reader=Bad)
        self.assertGreater(r['reasons']['pts_missing'],0)
        self.assertNotIn('detection_with_scheduler',r['stage_totals_ms'])

    def test_model_exception_redacted_cleanup(self):
        p=processor()
        def failed(_):raise RuntimeError('SECRET')
        p.engine.detect_faces=failed
        r=self.run_test(p)
        self.assertEqual(r['error'],'diagnostic_processing_failed')
        self.assertEqual(r['last_stage'],'detection_with_scheduler')
        self.assertTrue(r['decoder']['decoder_stopped'])
        self.assertNotIn('SECRET',str(r))

    def test_stricter_liveness_count_kept(self):
        p=processor();p.liveness.required_frames=4
        r=self.run_test(p)
        self.assertEqual([x['samples'] for x in r['trials']],[4,4,4])

    def test_unknown_identity_not_a_valid_trial(self):
        p=processor();p.gallery.match_candidates=lambda *a,**k:[]
        r=self.run_test(p,seconds=.25)
        self.assertEqual(r['trials'],[])
        self.assertGreater(r['reasons']['identity_unknown'],0)

    def test_duplicate_pixels_do_not_supply_three_samples(self):
        class Duplicate(Reader):
            def take(self):
                item,stats=super().take()
                if item:item=(*item[:4],np.zeros((100,100,3),dtype=np.uint8))
                return item,stats
        r=self.run_test(processor(),seconds=.25,reader=Duplicate)
        self.assertEqual(r['trials'],[])
        self.assertGreater(r['reasons']['duplicate_pixels'],0)

    def test_low_quality_stops_before_liveness_or_embedding(self):
        with patch.object(module,'measure_face_quality',return_value=NS(usable=False,to_dict=lambda:{'score':.1})):
            r=module.run_decode(processor(),'Enterance','rtsp://frigate/a',seconds=.15,reader_factory=Reader)
        self.assertGreater(r['reasons']['low_quality'],0)
        self.assertNotIn('embedding_total',r['stage_totals_ms'])

    def test_subject_changes_reset_trial(self):
        p=processor();calls=[]
        def candidates(*a,**k):
            calls.append(1)
            return [(str(len(calls)%2),'Name',.9)]
        p.gallery.match_candidates=candidates
        r=self.run_test(p,seconds=.4)
        self.assertEqual(r['trials'],[])

    def test_disabled_liveness_never_completes(self):
        p=processor();p.liveness.analyze=lambda *a:{'state':'disabled','live':None}
        r=self.run_test(p,seconds=.25)
        self.assertEqual(r['trials'],[])
        self.assertGreater(r['reasons']['liveness_disabled'],0)

    def test_runtime_engine_loss_aborts_and_releases_frames(self):
        p=processor()
        def detect(_):
            p.engine=None
            return []
        p.engine.detect_faces=detect
        r=self.run_test(p)
        self.assertTrue(r['decoder']['decoder_stopped'])
        self.assertIn('face_engine_unavailable',r['blockers'])

    def test_no_output_or_event_pipeline_calls(self):
        tree=ast.parse(Path(module.__file__).read_text())
        calls={n.func.attr for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute)}
        self.assertFalse(calls & {'submit','present','publish','_process_face','_recognize','add_face','imwrite','save_evidence'})

if __name__=='__main__':unittest.main()
