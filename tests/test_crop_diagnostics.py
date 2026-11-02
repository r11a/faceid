import json
import time
from types import SimpleNamespace as NS
from unittest import TestCase
from unittest.mock import Mock,patch
import numpy as np
from fastapi import HTTPException
from app import crop_diagnostics as crop
from app import decode_diagnostics as decode
from app import embedding_benchmark as bench
from test_decode_diagnostics import Reader,processor


def face(box=(5,5,35,35)):
    return NS(bbox=np.array(box,dtype=float),kps=np.array([[10,10],[20,10],[15,15],[10,20],[20,20]],dtype=float))


class CropTests(TestCase):
    def test_installed_insightface_face_copy_and_mapping(self):
        from insightface.app.common import Face
        original=Face(bbox=np.array([5.,5.,35.,35.]),kps=np.ones((5,2)),det_score=.9)
        engine=NS(detect_faces=Mock(side_effect=[[],[original]]))
        found,_=crop.compare(engine,np.zeros((200,400,3),np.uint8),[.25,.25,.75,.75])
        self.assertIsInstance(found[0],Face)
        np.testing.assert_equal(found[0].bbox,[105,55,135,85])
        np.testing.assert_equal(found[0].kps,np.ones((5,2))+[100,50])
        np.testing.assert_equal(original.bbox,[5,5,35,35])
    def test_invalid_regions_rejected(self):
        for value in (None,{},[0,0,1],[-.1,0,1,1],[False,0,1,1],[0,0,float('nan'),1],[.8,0,.2,1],[0,0,.01,1]):
            with self.assertRaises(ValueError):crop.validate_region(value)
    def test_coordinate_mapping_preserves_original_face_size_and_landmarks(self):
        original=face();engine=NS(detect_faces=Mock(side_effect=[[],[original]]))
        found,report=crop.compare(engine,np.zeros((200,400,3),np.uint8),[.25,.25,.75,.75])
        np.testing.assert_equal(found[0].bbox,[105,55,135,85])
        np.testing.assert_equal(found[0].kps,original.kps+[100,50])
        np.testing.assert_equal(original.bbox,[5,5,35,35])
        self.assertEqual(report['selected'],'crop');self.assertEqual(report['crop_size'],[200,100])
    def test_full_frame_ambiguity_is_not_hidden_by_crop(self):
        engine=NS(detect_faces=Mock(side_effect=[[face(),face()],[face()]]))
        found,_=crop.compare(engine,np.zeros((100,100,3),np.uint8),[.2,.2,.8,.8])
        self.assertEqual(len(found),2)
    def test_conflicting_locations_fail_closed(self):
        engine=NS(detect_faces=Mock(side_effect=[[face((0,0,20,20))],[face()]]))
        found,result=crop.compare(engine,np.zeros((200,200,3),np.uint8),[.5,.5,1,1])
        self.assertEqual(len(found),2);self.assertEqual(result['selected'],'conflicting_locations')
    def test_crop_first_alternates_without_changing_results(self):
        engine=NS(detect_faces=Mock(side_effect=[[face()],[]]))
        found,result=crop.compare(engine,np.zeros((100,100,3),np.uint8),[.2,.2,.8,.8],True)
        self.assertEqual(result['order'],'crop_first');self.assertEqual(len(found),1)
        self.assertEqual(engine.detect_faces.call_args_list[0].args[0].shape,(60,60,3))
    def test_input_preview_matches_aspect_preserving_detector_padding(self):
        image=crop.detector_input(np.full((1920,2592,3),255,dtype=np.uint8))
        self.assertEqual(image.shape,(640,640,3))
        self.assertTrue(np.all(image[:474]==255));self.assertTrue(np.all(image[474:]==0))
    def test_preview_owner_expiry_and_clear(self):
        cache=crop.PreviewCache(ttl=.03)
        cache.put('owner',{'scene':np.zeros((50,50,3),np.uint8)})
        self.assertTrue(cache.get('owner','scene').startswith(b'\xff\xd8'))
        with self.assertRaises(HTTPException):cache.get('other','scene')
        self.assertFalse(cache.status('other')['available'])
        time.sleep(.06);self.assertEqual(cache.items,{})
        with self.assertRaises(HTTPException):cache.get('owner','scene')
        cache.put('owner',{'scene':np.zeros((50,50,3),np.uint8)});cache.clear()
        self.assertFalse(cache.status('owner')['available'])
    def test_empty_search_passes_twenty_four_without_spending_face_budget(self):
        p=processor();p.engine.detect_faces=lambda _:[]
        with patch.object(decode.time, 'sleep'):
            result=decode.run_decode(p,'Enterance','rtsp://frigate/a',seconds=2,reader_factory=Reader,sample_interval=0)
        self.assertGreater(len(result['samples']),24)
        self.assertEqual(result['single_face_samples'],0)
        self.assertFalse(result['admission_eligible'])
    def test_full_image_used_for_quality_liveness_embedding_after_crop(self):
        p=processor();f=face();seen=[]
        p.engine.detect_faces=lambda image:[] if image.shape[0]==100 else [f]
        p.liveness.analyze=lambda image,face:seen.append(image.shape) or {'live':True,'state':'live'}
        with patch.object(decode,'measure_face_quality',return_value=NS(usable=True,to_dict=lambda:{})) as quality:
            result=decode.run_decode(p,'Enterance','rtsp://frigate/a',seconds=.2,reader_factory=Reader,
                sample_interval=.001,region=[.2,.2,.8,.8])
        self.assertTrue(seen);self.assertTrue(all(shape==(100,100,3) for shape in seen))
        self.assertEqual(quality.call_args.args[0].shape,(100,100,3))
        self.assertIn('embedding_total',result['stage_totals_ms'])
    def test_preview_does_not_run_models_and_always_stops_reader(self):
        p=processor();p.engine.detect_faces=Mock(side_effect=AssertionError('must not run'))
        callback=Mock()
        result=decode.run_preview(p,'Enterance','rtsp://frigate/a',[.2,.2,.8,.8],callback,Reader,seconds=.05,sample_interval=.005)
        self.assertEqual(result['state'],'preview_inconclusive');self.assertTrue(result['decoder']['decoder_stopped'])
        p.engine.detect_faces.assert_not_called();self.assertTrue(callback.called)
        self.assertFalse(result['admission_eligible'])
    def test_preview_runs_with_connector_without_submitting(self):
        p=processor();p.access1.transport=object();p.access1.enqueue=Mock()
        result=decode.run_preview(p,'Enterance','rtsp://frigate/a',[0,0,1,1],Mock(),Reader,seconds=.05,sample_interval=.005)
        self.assertNotIn('blockers',result)
        p.access1.enqueue.assert_not_called()
        self.assertFalse(result['admission_eligible'])
    def test_benchmark_follows_decoder_cleanup_without_an_access_path(self):
        p=processor();runner=Mock(return_value={'state':'test'});stopped=[]
        class CheckedReader(Reader):
            def stop(self):super().stop();stopped.append(True)
        def run(*a,**k):self.assertTrue(stopped);return {'state':'test'}
        runner.side_effect=run
        with patch.object(decode,'measure_face_quality',return_value=NS(usable=True,to_dict=lambda:{})),patch.object(bench,'prepare',return_value={'private':'never export'}):
            result=decode.run_decode(p,'Enterance','rtsp://frigate/a',seconds=.2,reader_factory=CheckedReader,
                                    benchmarks=True,benchmark_runner=runner,sample_interval=.001)
        runner.assert_called_once();self.assertNotIn('never export',json.dumps(result))
    def test_benchmark_is_bounded_and_only_aggregate_fields_exported(self):
        completed=NS(returncode=0,stdout=json.dumps({'state':'completed','median_ms':12,'image':'SECRET','reference':'SECRET'}))
        with patch.object(bench.subprocess,'run',return_value=completed) as run:
            report=bench.run({'image':'SECRET','reference':[1]})
        self.assertEqual(run.call_count,2);self.assertNotIn('SECRET',json.dumps(report))
        for call in run.call_args_list:self.assertEqual(call.kwargs['timeout'],15)
    def test_benchmark_timeout_and_safety_are_fail_closed(self):
        with patch.object(bench.subprocess,'run',side_effect=bench.subprocess.TimeoutExpired('test',15)):
            report=bench.run({'image':'SECRET'})
        self.assertEqual(report['state'],'incomplete');self.assertNotIn('SECRET',json.dumps(report))
        with patch.object(bench.subprocess,'run') as run:
            result=bench.run({'image':'SECRET'},allowed=lambda:False)
        run.assert_not_called();self.assertEqual(result['state'],'safety_changed')
