"""6.7.6: anchor grading and refresh, capture folding, warm readers, static OpenVINO shapes."""
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

import app.stream_observation as observation
from app import recognition_capture as capture_module
from app.live_frames import LiveFramePool, LiveFrameReader
from app.native_openvino import NativeSession, prepare_sessions
from app.quality import measure_face_quality
from app.subject_association import anchor_grade
import test_recognition_capture as capture_tests  # fixtures only; module imports avoid re-collection
import test_stream_observation as stream_tests


def landmark_face(nose_y):
    kps = np.array([[40, 40], [80, 40], [60, nose_y], [45, 80], [75, 80]], np.float32)
    return SimpleNamespace(bbox=np.array([10, 10, 110, 110], np.float32), kps=kps, det_score=.95)


class AnchorGradeTests(unittest.TestCase):
    def test_pitch_is_zero_frontal_and_positive_looking_down(self):
        image = np.random.default_rng(1).integers(0, 255, (128, 128, 3), dtype=np.uint8)
        frontal = measure_face_quality(image, landmark_face(59.6))
        down = measure_face_quality(image, landmark_face(74))
        up = measure_face_quality(image, landmark_face(48))
        self.assertAlmostEqual(frontal.pitch, 0.0, places=2)
        self.assertGreater(down.pitch, .17)
        self.assertLess(up.pitch, -.19)

    def test_pitch_is_absent_without_five_landmarks(self):
        face = SimpleNamespace(bbox=np.array([0, 0, 60, 60], np.float32), det_score=.9, kps=None)
        self.assertIsNone(measure_face_quality(np.zeros((64, 64, 3), np.uint8), face).pitch)

    def test_blur_or_head_down_makes_an_anchor_weak_but_usable(self):
        good = {'anchor_sharpness': .6, 'anchor_frontal': .8, 'anchor_pitch': .02, 'anchor_quality': .8}
        self.assertEqual(anchor_grade(good), (True, .8))
        self.assertFalse(anchor_grade({**good, 'anchor_sharpness': .1})[0])
        strong, rank = anchor_grade({**good, 'anchor_pitch': .3})
        self.assertFalse(strong)
        self.assertLess(rank, .8)
        self.assertFalse(anchor_grade({k: v for k, v in good.items() if k != 'anchor_pitch'})[0])


class AnchorRefreshTests(unittest.TestCase):
    setUp = stream_tests.StreamTests.setUp
    face = stream_tests.StreamTests.face
    trigger = stream_tests.StreamTests.trigger
    frame = stream_tests.StreamTests.frame

    def run_frames(self, count, grades, matching=False, distinct_crops=True):
        calls = self.frigate.association_frame
        if distinct_crops:
            def crop():
                image = np.zeros((100, 100, 3), np.uint8)
                image[99, 99, 0] = calls.call_count % 250
                return image
            calls.side_effect = lambda *args: crop()

        def grade(_details):
            return grades[min(calls.call_count, len(grades)) - 1]

        with patch.object(observation, 'ANCHOR_REFRESH_INTERVAL', 0), \
                patch.object(observation, 'anchor_grade', side_effect=grade):
            for n in range(1, count + 1):
                self.frame(n, faces=[self.face(n, other=not matching)])

    def stages(self, session):
        return [(item['stage'], item.get('reason')) for item in session['timeline']]

    def test_weak_anchor_is_replaced_by_a_strong_crop(self):
        s = self.trigger()
        self.run_frames(2, [(False, .3), (True, .9)])
        self.assertEqual(self.frigate.association_frame.call_count, 2)
        self.assertTrue(s['anchor_strong'])
        self.assertIn(('association_anchor_refreshed', 'strong'), self.stages(s))
        self.assertIn(('evidence_reset', 'anchor_refreshed'), self.stages(s))

    def test_unmatched_strong_anchor_is_refreshed_and_capped(self):
        s = self.trigger()
        self.run_frames(20, [(True, .9)])
        self.assertEqual(self.frigate.association_frame.call_count, 1 + observation.ANCHOR_REFRESH_LIMIT)
        self.assertEqual(s['anchor_refreshes'], observation.ANCHOR_REFRESH_LIMIT)
        self.assertEqual(s['st'].get('stream_samples', 0), 0)

    def test_unchanged_frigate_crop_skips_the_model_and_keeps_the_budget(self):
        s = self.trigger()
        with patch.object(observation, 'single_anchor', wraps=observation.single_anchor) as model:
            self.run_frames(6, [(False, .3)], distinct_crops=False)
        self.assertGreater(self.frigate.association_frame.call_count, 3)
        self.assertEqual(model.call_count, 1)
        self.assertEqual(s['anchor_refreshes'], 0)
        self.assertIn(('anchor_refresh_rejected', 'anchor_unchanged'), self.stages(s))

    def test_worse_crop_never_replaces_the_anchor(self):
        s = self.trigger()
        self.run_frames(2, [(False, .5), (False, .4)])
        first = s['anchor']
        self.assertIn(('anchor_refresh_rejected', 'anchor_not_better'), self.stages(s))
        self.assertIs(s['anchor'], first)

    def test_associated_subject_keeps_its_anchor_and_evidence(self):
        s = self.trigger()
        self.run_frames(3, [(False, .3)], matching=True)
        self.assertEqual(self.frigate.association_frame.call_count, 1)
        self.assertEqual(s['st']['stream_samples'], 3)


class BoxAssociationTests(unittest.TestCase):
    """A lone person event binds the one face inside Frigate's current person box."""
    setUp = stream_tests.StreamTests.setUp
    face = stream_tests.StreamTests.face
    trigger = stream_tests.StreamTests.trigger
    frame = stream_tests.StreamTests.frame

    def start(self, box=(0, 0, 100, 100), age=0.0, enabled=True, eid='event-1'):
        self.p.box_association = enabled
        self.frigate.detect_size = lambda camera: (100, 100)
        return self.trigger(eid=eid, box=list(box), frame_time=time.time() - age)

    def walk(self, frames=4, bbox=None):
        # The anchor never matches: only the box rule can associate these faces.
        for n in range(1, frames + 1):
            self.frame(n, faces=[self.face(n, bbox=bbox, other=True)])

    def test_face_inside_fresh_box_is_associated_without_anchor_match(self):
        s = self.start()
        self.walk()
        self.assertGreaterEqual(s['st']['stream_samples'], 3)

    def test_face_outside_box_stale_box_or_disabled_rule_is_not_associated(self):
        for kwargs, bbox in (({'box': (0, 0, 30, 30)}, [70, 70, 98, 98]), ({'age': 10.0}, None),
                             ({'enabled': False}, None)):
            with self.subTest(**kwargs):
                s = self.start(**kwargs, eid='event-%d' % len(self.m.seen))
                self.walk(bbox=bbox)
                self.assertEqual(s['st'].get('stream_samples', 0), 0)
                self.m.finish(s, 'test_done')

    def test_box_bound_face_keeps_evidence_when_the_tracker_rekeys_it(self):
        # Far/angled to near/frontal: each frame differs from the track's first
        # sighting (cosine 0.5 < 0.65), so the tracker issues a new key every frame.
        import math
        s = self.start()
        for n in range(1, 5):
            angle = math.radians(90 + 60 * n)
            vector = np.array([math.cos(angle), math.sin(angle)], np.float32)
            face = SimpleNamespace(bbox=np.array([0, 0, 80, 80], np.float32),
                                   normed_embedding=vector, det_score=.99)
            self.frame(n, faces=[face])
        stages = [(item['stage'], item.get('reason')) for item in s['timeline']]
        self.assertIn(('face_track_rebound', None), stages)
        self.assertNotIn(('evidence_reset', 'face_track_changed'), stages)
        self.assertGreaterEqual(s['st']['stream_samples'], 3)

    def test_every_sample_needs_a_fresh_box_even_for_a_bound_face(self):
        s = self.start()
        self.walk(frames=2)
        self.assertEqual(s['st']['stream_samples'], 2)
        s['frigate_box']['frame_time'] = time.time() - 10  # Frigate stopped updating the box.
        for n in (3, 4):  # The same face, but no fresh box to vouch for it.
            self.frame(n, faces=[self.face(n, other=True)])
        self.assertLess(s['st'].get('stream_samples', 0), 3)

    def test_a_replacement_person_in_the_same_spot_is_not_inherited(self):
        s = self.start()
        self.walk(frames=2)
        s['frigate_box']['frame_time'] = time.time() - 10
        for n in range(3, 6):  # Another face, same place, no fresh box for it.
            self.frame(n, faces=[SimpleNamespace(bbox=np.array([0, 0, 80, 80], np.float32),
                                                 normed_embedding=np.array([0., 1.], np.float32),
                                                 det_score=.99)])
        self.assertLess(s['st'].get('stream_samples', 0), 3)

    def test_a_frame_from_before_the_event_is_never_evidence(self):
        s = self.start()
        self.faces = [self.face(1, other=True)]
        self.reader.record = {'sequence': 1, 'generation': self.reader.generation,
                              'arrived': s['started'] - .2, 'pts_ms': 1250,
                              'image': np.full((100, 100, 3), 1, np.uint8)}
        self.m._step('front')
        self.assertEqual(s['st'].get('stream_samples', 0), 0)
        self.frame(2, faces=[self.face(2, other=True)])  # A frame after the event starts counts.
        self.assertEqual(s['st']['stream_samples'], 1)

    def test_second_person_event_disables_the_box_rule(self):
        first = self.start()
        self.start(eid='event-2')
        self.walk()
        self.assertEqual(first['st'].get('stream_samples', 0), 0)

    def test_two_faces_in_frame_disable_the_box_rule(self):
        s = self.start()
        for n in range(1, 5):
            self.frame(n, faces=[self.face(n, other=True), self.face(n + 40, bbox=[85, 85, 99, 99], other=True)])
        self.assertEqual(s['st'].get('stream_samples', 0), 0)


class AnchorTimingTests(unittest.TestCase):
    """6.7.8: quick retry while Frigate has not stored the event; box evidence before the anchor."""
    setUp = stream_tests.StreamTests.setUp
    face = stream_tests.StreamTests.face
    trigger = stream_tests.StreamTests.trigger
    frame = stream_tests.StreamTests.frame

    def test_not_ready_event_is_retried_quickly_until_frigate_stores_it(self):
        image = np.zeros((100, 100, 3), np.uint8)
        self.frigate.association_frame.side_effect = [None] * 5 + [image]
        s = self.trigger()
        with patch.object(observation, 'ANCHOR_TRANSIENT_INTERVAL', 0):
            for n in range(1, 10):
                self.frame(n)
        self.assertEqual(self.frigate.association_frame.call_count, 6)
        self.assertIsNotNone(s['anchor'])
        self.assertIn('event-1', self.m.sessions)

    def test_retry_interval_is_short_and_bounded(self):
        self.assertLessEqual(observation.ANCHOR_TRANSIENT_INTERVAL, 0.5)
        self.frigate.association_frame.side_effect = lambda *args: None
        self.trigger()
        with patch.object(observation, 'ANCHOR_TRANSIENT_INTERVAL', 0):
            for n in range(1, observation.ANCHOR_TRANSIENT_RETRIES + 4):
                self.frame(n)
        self.assertNotIn('event-1', self.m.sessions)
        self.assertEqual(self.frigate.association_frame.call_count, observation.ANCHOR_TRANSIENT_RETRIES)

    def pending_anchor(self, box):
        from concurrent.futures import Future
        self.m.anchor_executor = SimpleNamespace(submit=lambda *args: Future())  # never completes
        self.p.box_association = True
        self.frigate.detect_size = lambda camera: (100, 100)
        return self.trigger(box=list(box), frame_time=time.time())

    def test_lone_face_in_fresh_box_collects_evidence_before_the_anchor(self):
        s = self.pending_anchor((0, 0, 100, 100))
        for n in range(1, 5):
            self.frame(n, faces=[self.face(n, other=True)])
        self.assertIsNone(s['anchor'])
        self.assertGreaterEqual(s['st']['stream_samples'], 3)

    def test_face_outside_box_waits_for_the_anchor(self):
        s = self.pending_anchor((0, 0, 30, 30))
        for n in range(1, 5):
            self.frame(n, faces=[self.face(n, bbox=[70, 70, 98, 98], other=True)])
        self.assertEqual(s['st'].get('stream_samples', 0), 0)
        self.assertIsNotNone(s.get('early_candidate'))


class CaptureFoldingTests(unittest.TestCase):
    setUp = capture_tests.CaptureTests.setUp
    start = capture_tests.CaptureTests.start

    def test_repeated_snapshot_skips_fold_into_one_counted_record(self):
        self.start()
        for index in range(5):
            capture_module.emit(self.processor, 'e1', 'front', 'snapshot_skipped', reason='mqtt_update_missing')
            capture_module.emit(self.processor, 'e1', 'front', 'frame', sequence=index)
            self.clock[0] += .25
        records = self.capture.report('admin')['events'][0]['records']
        skips = [r for r in records if r['stage'] == 'snapshot_skipped']
        self.assertEqual(len(skips), 1)
        self.assertEqual(skips[0]['repeat_count'], 5)
        self.assertEqual(skips[0]['last_elapsed_ms'], 1000.0)

    def test_lifecycle_records_survive_a_record_flood(self):
        self.start()
        capture_module.emit(self.processor, 'e1', 'front', 'frigate_event_received')
        capture_module.emit(self.processor, 'e1', 'front', 'stream_trigger_received')
        capture_module.emit(self.processor, 'e1', 'front', 'association_anchor_model', anchor_model_ms=280)
        for index in range(capture_module.MAX_RECORDS + 50):
            capture_module.emit(self.processor, 'e1', 'front', 'frame', sequence=index)
        stages = [r['stage'] for r in self.capture.report('admin')['events'][0]['records']]
        self.assertEqual(len(stages), capture_module.MAX_RECORDS)
        self.assertEqual(stages[:3], ['frigate_event_received', 'stream_trigger_received',
                                      'association_anchor_model'])


class WarmReaderTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(LiveFrameReader, 'start')
        patcher.start()
        self.addCleanup(patcher.stop)

    def pool(self, linger):
        pool = LiveFramePool(observation_linger=linger)
        self.addCleanup(pool.close)
        return pool

    def test_released_reader_stays_warm_and_is_reused(self):
        pool = self.pool(30)
        reader = pool.acquire_observation('door', 'rtsp://fixture/door')
        pool.release_observation('door')
        self.assertFalse(reader.stopped.is_set())
        self.assertGreater(pool.report()['door']['warm_until_s'], 0)
        pool.sync({})
        self.assertIs(pool.acquire_observation('door', 'rtsp://fixture/door'), reader)
        self.assertNotIn('door', pool.lingering)

    def test_warm_reader_expires(self):
        pool = self.pool(30)
        reader = pool.acquire_observation('door', 'rtsp://fixture/door')
        pool.release_observation('door')
        with patch('app.live_frames.time.monotonic', return_value=float('inf')):
            pool._expire_linger()
        self.assertTrue(reader.stopped.is_set())
        self.assertEqual(pool.report(), {})

    def test_zero_linger_stops_immediately_and_only_one_reader_stays_warm(self):
        pool = self.pool(0)
        reader = pool.acquire_observation('door', 'rtsp://fixture/door')
        pool.release_observation('door')
        self.assertTrue(reader.stopped.is_set())
        pool = self.pool(30)
        first = pool.acquire_observation('door', 'rtsp://fixture/door')
        second = pool.acquire_observation('gate', 'rtsp://fixture/gate')
        pool.release_observation('door')
        pool.release_observation('gate')
        self.assertTrue(first.stopped.is_set())
        self.assertFalse(second.stopped.is_set())
        self.assertEqual(list(pool.lingering), ['gate'])


class StaticOpenVINOTests(unittest.TestCase):
    def test_static_shape_is_compiled_and_batches_run_per_row(self):
        graph = Mock()
        model = SimpleNamespace(model_file='w600k.onnx', session=SimpleNamespace(
            get_inputs=lambda: [SimpleNamespace(name='input.1')],
            get_outputs=lambda: [SimpleNamespace(name='683')]))

        class Compiled:
            def get_property(self, name):
                return ['GPU.0']

            def input(self, name):
                return name

            def output(self, name):
                return name

            def __call__(self, inputs):
                value = inputs['input.1']
                assert len(value) == 1
                return {'683': value.reshape(1, -1)[:, :2] + 1}

        configs = []
        core = SimpleNamespace(read_model=lambda path: graph,
                               compile_model=lambda g, device, config: configs.append(config) or Compiled())
        session = NativeSession(core, model, 'GPU', (1, 3, 112, 112), {'X': 1})
        graph.reshape.assert_called_once_with({'input.1': [1, 3, 112, 112]})
        self.assertEqual(configs[0]['X'], 1)
        out = session.run(None, {'input.1': np.arange(6, dtype=np.float32).reshape(3, 2)})[0]
        np.testing.assert_array_equal(out, [[1, 2], [3, 4], [5, 6]])

    def test_cpu_thread_cap_and_static_shapes_are_used(self):
        created = []

        class FakeSession:
            def __init__(self, core, model, device, shape, config):
                created.append((device, shape, config))
                self.inputs = [SimpleNamespace(name='x')]

            def run(self, names, feed):
                return [np.ones(3, np.float32)]

        models = {'detection': SimpleNamespace(), 'recognition': SimpleNamespace(input_size=(112, 112))}
        with patch.dict(sys.modules, {'openvino': SimpleNamespace(Core=lambda: Mock())}), \
                patch('app.native_openvino.NativeSession', FakeSession):
            prepare_sessions(models, 'CPU', 640, '/tmp/cache', cpu_threads=2)
        self.assertEqual(created, [('CPU', (1, 3, 640, 640), {'INFERENCE_NUM_THREADS': 2}),
                                   ('CPU', (1, 3, 112, 112), {'INFERENCE_NUM_THREADS': 2})])


if __name__ == '__main__':
    unittest.main()
