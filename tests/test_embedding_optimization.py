"""No network, real cameras, identities or relay calls."""
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
import numpy as np
import test_cpu_efficiency as cpu
import test_live_access_bench as sampling
import test_live_consensus_bench as consensus


class EmbeddingOptimizationTests(unittest.TestCase):
    def engine(self):
        return cpu.CpuEfficiencyTests().engine()

    def test_embedding_reports_wait_and_compute_without_changing_vector(self):
        engine,_,_,embed=self.engine()
        image=np.zeros((100,100,3),dtype=np.uint8)
        face=engine.detect_faces(image)[0];timings={}
        engine.embed_face(image,face,timings=timings)
        self.assertEqual(set(timings),{'embedding_scheduler_wait','embedding_compute'})
        self.assertTrue(all(value>=0 for value in timings.values()))
        np.testing.assert_array_equal(face.embedding,[1,1])
        self.assertEqual(engine.health()['embedding']['calls'],1)
        self.assertEqual(engine.health()['embedding']['failures'],0)
        engine.embed_face(image,face,timings=timings)
        self.assertEqual(timings,{})
        self.assertEqual(embed.call_count,1)

    def test_two_callers_same_face_compute_only_once(self):
        engine,_,_,embed=self.engine()
        image=np.zeros((100,100,3),dtype=np.uint8)
        face=engine.detect_faces(image)[0]
        entered,release,second_waiting=threading.Event(),threading.Event(),threading.Event()
        original=embed.side_effect
        def calculate(*args):
            entered.set()
            if not release.wait(3):raise RuntimeError('test release timeout')
            original(*args)
        embed.side_effect=calculate
        errors=[];timings={}
        def run(first):
            try:
                if not first:second_waiting.set()
                engine.embed_face(image,face,timings=None if first else timings)
            except Exception as exc:errors.append(exc)
        a=threading.Thread(target=run,args=(True,));b=threading.Thread(target=run,args=(False,))
        a.start()
        try:
            self.assertTrue(entered.wait(2));b.start()
            self.assertTrue(second_waiting.wait(2))
            # Wait until it is queued on the real scheduler, not just launched.
            deadline=time.monotonic()+2
            while engine._lock._live_waiters==0 and time.monotonic()<deadline:time.sleep(.005)
            self.assertEqual(engine._lock._live_waiters,1)
        finally:
            release.set();a.join(3)
            if b.ident is not None:b.join(3)
        self.assertEqual(errors,[])
        self.assertFalse(a.is_alive() or b.is_alive())
        self.assertEqual(embed.call_count,1)
        self.assertIn('embedding_scheduler_wait',timings)
        self.assertNotIn('embedding_compute',timings)

    def test_embedding_error_records_failure_and_releases_scheduler(self):
        engine,_,_,embed=self.engine();image=np.zeros((100,100,3),dtype=np.uint8)
        face=engine.detect_faces(image)[0];timings={}
        embed.side_effect=ValueError('model failure')
        with self.assertRaises(ValueError):engine.embed_face(image,face,timings=timings)
        self.assertEqual(engine.health()['embedding']['failures'],1)
        self.assertIn('embedding_compute',timings)
        self.assertFalse(engine._lock._busy)
        self.assertIsNone(face.embedding)

    def test_embedding_metrics_are_bounded(self):
        engine,_,_,_=self.engine();image=np.zeros((100,100,3),dtype=np.uint8)
        for _ in range(300):engine.embed_face(image,SimpleNamespace(embedding=None,bbox=[0,0,80,80]))
        health=engine.health()['embedding']
        self.assertEqual(health['calls'],300)
        self.assertEqual(health['samples'],256)

    def test_warmup_uses_model_preprocessing_without_creating_face_evidence(self):
        engine,_,_,embed=self.engine();model=engine.app.models['recognition']
        model.input_size=(112,112);model.get_feat=Mock(return_value=np.ones((1,512),dtype=np.float32))
        engine._prepare_embedding()
        self.assertEqual(engine.embedding_warmup['state'],'ready')
        image=model.get_feat.call_args.args[0]
        self.assertEqual(image.shape,(112,112,3));self.assertEqual(image.dtype,np.uint8)
        self.assertFalse(image.any());embed.assert_not_called()
        self.assertEqual(engine.health()['embedding']['calls'],0)

    def test_warmup_failure_is_not_cached_as_recognition(self):
        engine,_,_,embed=self.engine();model=engine.app.models['recognition']
        model.input_size=(112,112);model.get_feat=Mock(side_effect=RuntimeError('PRIVATE'))
        with self.assertLogs('faceid.engine',level='WARNING') as captured:engine._prepare_embedding()
        self.assertEqual(engine.embedding_warmup['state'],'failed')
        self.assertNotIn('PRIVATE',str(captured.output))
        image=np.zeros((100,100,3),dtype=np.uint8)
        engine.embed_face(image,engine.detect_faces(image)[0]);embed.assert_called_once()

    def test_warmup_skips_accelerated_provider_and_invalid_shape(self):
        engine,_,_,_=self.engine();model=engine.app.models['recognition']
        model.input_size=(112,112);model.get_feat=Mock()
        engine.providers=['CUDAExecutionProvider','CPUExecutionProvider'];engine._prepare_embedding()
        self.assertEqual(engine.embedding_warmup['state'],'skipped_accelerated')
        model.get_feat.assert_not_called()
        engine.providers=['CPUExecutionProvider'];model.input_size=(4096,4096);engine._prepare_embedding()
        self.assertEqual(engine.embedding_warmup['state'],'unsupported');model.get_feat.assert_not_called()

    def test_no_scheduler_jobs_for_stale_or_invalid_timestamps(self):
        for stamp in (None,True,'100',float('nan'),float('inf'),time.time()-11,time.time()+10):
            with self.subTest(stamp=stamp):
                p,st=sampling.LiveSamplingTests().processor();st['live_snapshot_ts']=stamp
                for _ in range(5):p._schedule_live_jobs()
                self.assertTrue(p.queue.empty());self.assertEqual(st['attempts'],0)
                p.frigate.live_event_frame.assert_not_called()

    def test_new_capture_resumes_sampling_without_counting_consumed_frame(self):
        p,st=sampling.LiveSamplingTests().processor()
        st['live_snapshot_consumed_ts']=st['live_snapshot_ts']
        p._schedule_live_jobs();self.assertTrue(p.queue.empty())
        st['live_snapshot_ts']=time.time();p._schedule_live_jobs()
        self.assertEqual(p.queue.qsize(),1);self.assertEqual(st['attempts'],0)

    def test_execution_rechecks_freshness_after_queueing(self):
        p,st=sampling.LiveSamplingTests().processor();p._schedule_live_jobs()
        self.assertEqual(p.queue.qsize(),1)
        st['live_snapshot_ts']=time.time()-11
        p._work_once(p.queue)
        p.frigate.live_event_frame.assert_not_called();self.assertEqual(st['attempts'],0)

    def test_notification_only_sampling_does_not_gain_access_timestamp_requirement(self):
        p,st=sampling.LiveSamplingTests().processor()
        p.access1.transport=None;st['live_snapshot_ts']=None
        p._schedule_live_jobs();self.assertEqual(p.queue.qsize(),1)

    def test_timed_embedding_still_requires_three_live_frames(self):
        fixture=consensus.LiveConsensusTests();p,st=fixture.setup_processor()
        p.timing_diagnostics=True;p.engine.supports_embedding_timing=True
        def embed(image,face,*,timings):
            timings.update(embedding_scheduler_wait=.002,embedding_compute=.01)
        p.engine.embed_face=Mock(side_effect=embed)
        with self.assertLogs('faceid.mqtt',level='INFO') as captured:
            fixture.frame(p,st,1);fixture.frame(p,st,2)
            p.access1.enqueue.assert_not_called()
            fixture.frame(p,st,3)
        p.access1.enqueue.assert_called_once()
        self.assertIn('stage=embedding_scheduler_wait',str(captured.output))
        self.assertIn('stage=embedding_compute',str(captured.output))


if __name__=='__main__':unittest.main()
