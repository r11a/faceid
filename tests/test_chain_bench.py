import threading
import time
import unittest
import sys
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock, patch
import numpy as np
from app.engine import FaceEngine, InferenceLock
import test_live_access_bench as sampling
import test_live_consensus_bench as consensus

class ChainSchedulingTests(unittest.TestCase):
    def test_background_cannot_enter_between_live_model_stages(self):
        lock=InferenceLock();started=threading.Event();entered=threading.Event()
        def background():
            with lock.background_work():
                started.set()
                with lock:entered.set()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with lock.live_work():
                future=pool.submit(background)
                self.assertTrue(started.wait(1))
                with lock:pass
                self.assertFalse(entered.wait(.05))
                with lock:pass
                self.assertFalse(entered.is_set())
            future.result(timeout=2)
            self.assertTrue(entered.is_set())

    def test_priority_reservation_released_after_exception(self):
        lock=InferenceLock()
        with self.assertRaises(ValueError):
            with lock.live_work():raise ValueError('test')
        self.assertEqual(lock._live_reservations,0)
        with lock.background_work():
            with lock:self.assertTrue(lock._busy)
        self.assertFalse(lock._busy)

    def test_liveness_uses_shared_model_scheduler(self):
        p,st=consensus.LiveConsensusTests().setup_processor()
        lock=InferenceLock();p.engine=SimpleNamespace(model_work=lambda:lock)
        def analyze(*args):
            self.assertTrue(lock._busy)
            return {'state':'live','live':True,'face_px':80,'score':.9}
        p.liveness.analyze=analyze
        consensus.LiveConsensusTests().frame(p,st,1)
        self.assertFalse(lock._busy)

    def test_live_worker_reservation_released_when_processing_fails(self):
        p,st=sampling.LiveSamplingTests().processor();lock=InferenceLock()
        p.engine.live_work=lock.live_work
        def fail(_):
            self.assertEqual(lock._live_reservations,1)
            raise ValueError('expected')
        p._process=fail;p._enqueue_job('e');p._work_once(p.queue)
        self.assertEqual(lock._live_reservations,0)
        self.assertEqual(p.queue.unfinished_tasks,0)

class EngineStageTests(unittest.TestCase):
    def engine(self):
        session=SimpleNamespace(get_providers=lambda:['CPUExecutionProvider'])
        detection=SimpleNamespace(model_file='detect.onnx',session=session,detect=Mock(return_value=['box']))
        recognition=SimpleNamespace(model_file='recognition.onnx',session=session,get=Mock(return_value='embedding'))
        output=object()
        def get(image):
            detection.detect(image,threshold=.5);recognition.get(image,'face');return output
        app=SimpleNamespace(prepare=lambda **kw:None,get=get,det_model=detection,models={'detection':detection,'recognition':recognition})
        with patch.dict(sys.modules,{'insightface.app':SimpleNamespace(FaceAnalysis=lambda **kw:app),
                                    'onnxruntime':SimpleNamespace(get_available_providers=lambda:['CPUExecutionProvider'])}):
            engine=FaceEngine()
        return engine,output

    def test_wrappers_preserve_results_and_separate_stages(self):
        engine,output=self.engine();image=np.zeros((10,10,3),dtype=np.uint8)
        self.assertIs(engine.faces(image),output)
        self.assertEqual(engine.health()['inference']['calls'],1)

    def test_wrapped_errors_propagate_and_release_scheduler(self):
        engine,_=self.engine()
        def fail(*a,**kw):raise ValueError('bad model')
        engine.app.det_model.detect=fail
        with self.assertRaisesRegex(ValueError,'bad model'):engine.faces(np.zeros((10,10,3)))
        self.assertFalse(engine._lock._busy)
        self.assertEqual(engine.health()['inference']['failures'],1)

if __name__=='__main__':unittest.main()
