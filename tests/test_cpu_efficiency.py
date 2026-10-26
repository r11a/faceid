import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
import numpy as np
import onnxruntime as ort
from insightface.app import FaceAnalysis
from app.engine import FaceEngine
import test_live_consensus_bench as consensus
import test_live_access_bench as sampling


class CpuEfficiencyTests(unittest.TestCase):
    def engine(self, **kwargs):
        session = SimpleNamespace(get_providers=lambda: ['CPUExecutionProvider'])
        boxes = np.array([[0, 0, 80, 80, .99], [10, 10, 70, 70, .98]], dtype=np.float32)
        points = np.arange(20, dtype=np.float32).reshape(2, 5, 2)
        detect = Mock(return_value=(boxes, points))
        detection = SimpleNamespace(model_file='detect.onnx', session=session, detect=detect)
        def embed(image, face):
            face.embedding = np.array([1., float(face.bbox[0])+1], dtype=np.float32)
        embed_mock = Mock(side_effect=embed)
        recognition = SimpleNamespace(model_file='recognition.onnx', session=session, get=embed_mock)
        app = SimpleNamespace(prepare=lambda **kw: None, det_model=detection,
                              models={'detection': detection, 'recognition': recognition}, addons={})
        app.get = lambda image: FaceAnalysis.get(app, image)
        factory = Mock(return_value=app)
        with patch('insightface.app.FaceAnalysis', factory), patch('onnxruntime.get_available_providers', return_value=['CPUExecutionProvider']):
            engine = FaceEngine(**kwargs)
        return engine, factory, detect, embed_mock

    def test_default_runtime_options_unchanged(self):
        engine, factory, _, _ = self.engine()
        self.assertNotIn('sess_options', factory.call_args.kwargs)
        self.assertFalse(engine.health()['cpu_tuning']['applied'])

    def test_cpu_options_reach_session_constructor(self):
        engine, factory, _, _ = self.engine(cpu_threads=2, cpu_allow_spinning=False)
        options = factory.call_args.kwargs['sess_options']
        self.assertIsInstance(options, ort.SessionOptions)
        self.assertEqual(options.intra_op_num_threads, 2)
        for key in ('session.intra_op.allow_spinning', 'session.inter_op.allow_spinning'):
            self.assertEqual(options.get_session_config_entry(key), '0')
        self.assertTrue(engine.health()['cpu_tuning']['applied'])

    def test_accelerated_backend_not_retuned(self):
        engine, factory, _, _ = self.engine(providers=['CUDAExecutionProvider', 'CPUExecutionProvider'], cpu_threads=2)
        self.assertNotIn('sess_options', factory.call_args.kwargs)
        self.assertFalse(engine.cpu_tuning_applied)

    def test_invalid_cpu_options_fail_explicitly(self):
        for value in (True, -1, 17, '2', 2.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.engine(cpu_threads=value)
        with self.assertRaises(ValueError):
            self.engine(cpu_allow_spinning='false')

    def test_deferred_path_matches_installed_analysis_contract(self):
        engine, _, detect, embed = self.engine()
        image = np.zeros((100, 100, 3), dtype=np.uint8)
        original = engine.faces(image)
        self.assertEqual(embed.call_count, 2)
        embed.reset_mock()
        deferred = engine.detect_faces(image)
        embed.assert_not_called()
        detect.assert_called_with(image, max_num=0, metric='default')
        for before, after in zip(original, deferred):
            np.testing.assert_array_equal(before.bbox, after.bbox)
            np.testing.assert_array_equal(before.kps, after.kps)
            self.assertEqual(before.det_score, after.det_score)
            engine.embed_face(image, after)
            np.testing.assert_array_equal(before.normed_embedding, after.normed_embedding)
        self.assertEqual(embed.call_count, 2)
        engine.embed_face(image, deferred[0])
        self.assertEqual(embed.call_count, 2)

    def test_only_selected_face_embedded(self):
        engine, _, _, embed = self.engine()
        image = np.zeros((100, 100, 3), dtype=np.uint8)
        faces = engine.detect_faces(image)
        engine.embed_face(image, faces[1])
        self.assertIsNone(faces[0].embedding)
        self.assertIsNotNone(faces[1].embedding)
        self.assertEqual(embed.call_count, 1)

    def test_embedding_failure_releases_scheduler(self):
        engine, _, _, embed = self.engine()
        image = np.zeros((100, 100, 3), dtype=np.uint8)
        face = engine.detect_faces(image)[0]
        embed.side_effect = ValueError('model failure')
        with self.assertRaisesRegex(ValueError, 'model failure'):
            engine.embed_face(image, face)
        self.assertFalse(engine._lock._busy)

    def test_rejected_liveness_skips_embedding_and_identity(self):
        helper = consensus.LiveConsensusTests()
        for result in ({'state': 'spoof', 'live': False, 'face_px': 80},
                       {'state': 'unavailable', 'live': None, 'face_px': 80},
                       {'state': 'insufficient', 'live': None, 'face_px': 30}):
            p, st = helper.setup_processor()
            p.engine.embed_face = Mock()
            p.liveness.analyze.return_value = result
            helper.frame(p, st, 1)
            p.engine.embed_face.assert_not_called()
            p.gallery.match_candidates.assert_not_called()
            p.access1.enqueue.assert_not_called()

    def test_live_consensus_still_requires_three_distinct_frames(self):
        helper = consensus.LiveConsensusTests()
        p, st = helper.setup_processor()
        p.engine.embed_face = Mock()
        for n in (1, 2):
            helper.frame(p, st, n)
            p.access1.enqueue.assert_not_called()
        helper.frame(p, st, 3)
        self.assertEqual(p.engine.embed_face.call_count, 3)
        p.access1.enqueue.assert_called_once()

    def test_archival_path_does_not_reembed(self):
        helper = consensus.LiveConsensusTests()
        p, st = helper.setup_processor()
        p.engine.embed_face = Mock()
        for n in (1, 2, 3):
            helper.frame(p, st, n, source='clip:1')
        p.engine.embed_face.assert_not_called()
        p.access1.enqueue.assert_not_called()

    def test_live_pipeline_rejects_low_quality_before_embedding(self):
        p, st = sampling.LiveSamplingTests().processor()
        p.frigate.live_event_frame.return_value = np.zeros((100, 100, 3), dtype=np.uint8)
        face = SimpleNamespace(bbox=np.array([0, 0, 80, 80]), det_score=.99)
        p.engine.detect_faces = Mock(return_value=[face])
        p.engine.embed_face = Mock()
        with patch('app.mqtt_listener.measure_face_quality', return_value=SimpleNamespace(usable=False, score=.1)):
            p._process('e')
        p.engine.detect_faces.assert_called_once()
        p.engine.faces.assert_not_called()
        p.engine.embed_face.assert_not_called()

    def test_live_pipeline_keeps_quality_selection_before_identity(self):
        p, st = sampling.LiveSamplingTests().processor()
        p.frigate.live_event_frame.return_value = np.zeros((100, 100, 3), dtype=np.uint8)
        faces = [SimpleNamespace(bbox=np.array([0, 0, 80, 80]), det_score=.99),
                 SimpleNamespace(bbox=np.array([10, 10, 70, 70]), det_score=.98)]
        p.engine.detect_faces = Mock(return_value=faces)
        p.engine.embed_face = Mock()
        p._process_face = Mock()
        with patch('app.mqtt_listener.measure_face_quality', side_effect=[
            SimpleNamespace(usable=True, score=.6), SimpleNamespace(usable=True, score=.9)]):
            p._process('e')
        p.engine.faces.assert_not_called()
        p.engine.embed_face.assert_not_called()
        self.assertIs(p._process_face.call_args.args[3], faces[1])
        self.assertEqual(p._process_face.call_args.kwargs['source'], 'secure_live_snapshot')


if __name__ == '__main__':
    unittest.main()
