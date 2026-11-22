"""Offline subject-attribution regressions; synthetic vectors are not field proof."""
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from app.liveness import LivenessDetector
from app.subject_association import usable_faces, single_anchor, mutual_assignments
import test_live_access_bench as sampling


def face(vector=(1, 0, 0), box=(0, 0, 80, 80), score=.8):
    return SimpleNamespace(bbox=np.array(box, dtype=float), det_score=.99,
                           normed_embedding=np.array(vector, dtype=np.float32), score=score)


def quality(image, item, **kwargs):
    return SimpleNamespace(usable=True, score=item.score,
                           to_dict=lambda: {'score': item.score, 'sharpness': .8})


class SubjectAssociationTests(unittest.TestCase):
    def test_event_anchor_is_exactly_one_usable_face_not_best_quality(self):
        engine = SimpleNamespace(detect_faces=Mock(), embed_face=Mock())
        image = np.zeros((100, 100, 3), dtype=np.uint8)
        with patch('app.subject_association.measure_face_quality', side_effect=quality):
            for faces in ([], [face(), face((0, 1, 0), score=.99)]):
                engine.detect_faces.return_value = faces
                anchor, reason = single_anchor(engine, image, {}, min_face_px=24, min_quality=.35)
                self.assertIsNone(anchor)
                self.assertEqual(reason, 'anchor_face_ambiguous_or_low_quality')
            engine.embed_face.assert_not_called()
            engine.detect_faces.return_value = [face((2, 0, 0))]
            anchor, reason = single_anchor(engine, image, {}, min_face_px=24, min_quality=.35)
            np.testing.assert_array_equal(anchor, [1, 0, 0])
            self.assertEqual(reason, 'association_anchor_ready')

    def test_crop_coordinates_do_not_use_full_frame_roi(self):
        image = np.zeros((100, 100, 3), dtype=np.uint8)
        profile = {'mode': 'intercom', 'roi': [.7, .7, 1, 1]}
        engine = SimpleNamespace(detect_faces=lambda _: [face()], embed_face=Mock())
        with patch('app.subject_association.measure_face_quality', side_effect=quality):
            self.assertEqual(usable_faces(image, [face()], profile,
                min_face_px=24, min_quality=.35), [])
            anchor, _ = single_anchor(engine, image, profile, min_face_px=24, min_quality=.35)
            self.assertIsNotNone(anchor)

    def test_two_subjects_assign_independently_of_detector_order(self):
        anchors = [[1, 0, 0], [0, 1, 0]]
        self.assertEqual(mutual_assignments(anchors, [face(), face((0, 1, 0))]), [0, 1])
        self.assertEqual(mutual_assignments(anchors, [face((0, 1, 0)), face()]), [1, 0])

    def test_competing_events_or_faces_cannot_share_an_assignment(self):
        self.assertEqual(mutual_assignments([[1, 0, 0], [1, 0, 0]], [face()]), [None, None])
        self.assertEqual(mutual_assignments([[1, 0, 0]], [face(), face()]), [None])
        self.assertEqual(mutual_assignments([[1, 0, 0]], [face((.4, .9, 0))]), [None])

    def test_invalid_vectors_and_mismatched_dimensions_fail_closed(self):
        for anchor in ([float('nan'), 0, 0], [0, 0, 0], [1, 0], None):
            self.assertEqual(mutual_assignments([anchor], [face()]), [None])
        for vector in ([float('inf'), 0, 0], [0, 0, 0]):
            self.assertEqual(mutual_assignments([[1, 0, 0]], [face(vector)]), [None])


class SnapshotAttributionTests(unittest.TestCase):
    def setUp(self):
        self.p, self.st = sampling.LiveSamplingTests().processor()
        self.profile = self.p.camera_profiles.get('front')
        self.profile['liveness_mode'] = 'required'
        self.p.camera_profiles.get = lambda _: self.profile
        self.anchor_image = np.full((100, 100, 3), 255, dtype=np.uint8)
        self.anchor_faces = [face()]
        self.live_faces = [face()]
        self.p.frigate.association_frame = Mock(return_value=self.anchor_image)
        self.p.engine.detect_faces = Mock(side_effect=lambda image:
            self.anchor_faces if image is self.anchor_image else self.live_faces)
        self.p.engine.embed_face = Mock()
        self.p._process_face = Mock()
        self.number = 0
        self.q1 = patch('app.subject_association.measure_face_quality', side_effect=quality)
        self.q2 = patch('app.mqtt_listener.measure_face_quality', side_effect=quality)
        self.q1.start(); self.q2.start()
        self.addCleanup(self.q1.stop); self.addCleanup(self.q2.stop)

    def run_frame(self, eid='e', faces=None):
        self.number += 1
        st = self.p.events[eid]
        if faces is not None:
            self.live_faces = faces
        self.p.frigate.live_event_frame.return_value = np.full((180, 320, 3), self.number, dtype=np.uint8)
        st['live_updated_at'] = time.time()
        # Strictly increasing even when two frames share one coarse clock tick (Windows).
        st['live_snapshot_ts'] = time.time() - .1 + self.number * 1e-3
        self.p._process(eid)

    def add_event(self, eid, anchor):
        st = self.p._new_event_state(eid, 'front', start_time=time.time() - 8)
        st.update(snapshot_anchor=np.asarray(anchor, dtype=float), snapshot_anchor_attempts=1,
                  snapshot_anchor_profile=self.p._snapshot_profile_key(self.profile))
        self.p.events[eid] = st
        return st

    def test_anchor_subject_wins_over_higher_quality_known_bystander(self):
        subject = face(score=.6)
        bystander = face((0, 1, 0), (200, 0, 280, 80), .99)
        self.run_frame(faces=[bystander, subject])
        self.assertIs(self.p._process_face.call_args.args[3], subject)
        self.p.frigate.association_frame.assert_called_once_with('e', 'front')
        self.assertEqual(self.st['liveness_history'], [])  # anchor is not evidence
        self.assertEqual(self.p.engine.embed_face.call_count, 3)

    def test_cached_anchor_and_detector_order_are_stable(self):
        subject = face()
        bystander = face((0, 1, 0), (200, 0, 280, 80))
        self.run_frame(faces=[bystander, subject])
        track = self.st['snapshot_subject_track']
        self.run_frame(faces=[subject, bystander])
        self.assertEqual(self.st['snapshot_subject_track'], track)
        self.assertIs(self.p._process_face.call_args.args[3], subject)
        self.p.frigate.association_frame.assert_called_once()

    def test_two_event_anchors_attribute_separate_subjects(self):
        other = self.add_event('other', [0, 1, 0])
        subject = face()
        bystander = face((0, 1, 0), (200, 0, 280, 80))
        self.run_frame(faces=[subject, bystander])
        self.assertIs(self.p._process_face.call_args.args[3], subject)
        self.run_frame('other', [bystander, subject])
        self.assertIs(self.p._process_face.call_args.args[3], bystander)
        self.assertNotEqual(other['snapshot_subject_track'], self.st['snapshot_subject_track'])

    def test_competing_event_anchor_blocks_instead_of_attributing_twice(self):
        self.add_event('other', [1, 0, 0])
        self.run_frame()
        self.p._process_face.assert_not_called()
        self.assertEqual(self.st['snapshot_association_reason'], 'association_ambiguous')

    def test_uncertain_other_anchor_does_not_block_clear_subject(self):
        self.add_event('other', [0, 0, 1])
        self.run_frame()
        self.p._process_face.assert_called_once()

    def test_multiple_anchor_faces_never_select_best_gallery_match(self):
        self.anchor_faces = [face(score=.6), face((0, 1, 0), score=.99)]
        self.run_frame()
        self.p._process_face.assert_not_called()
        self.p.engine.embed_face.assert_not_called()
        self.assertEqual(self.st['snapshot_association_reason'], 'anchor_face_ambiguous_or_low_quality')

    def test_anchor_fetch_is_bounded_to_two_attempts(self):
        self.p.frigate.association_frame.return_value = None
        self.run_frame()
        self.run_frame()
        self.assertEqual(self.p.frigate.association_frame.call_count, 1)
        self.st['snapshot_anchor_retry_at'] = time.monotonic() - 1
        self.run_frame()
        self.run_frame()
        self.assertEqual(self.p.frigate.association_frame.call_count, 2)
        self.p._process_face.assert_not_called()

    def test_anchor_retry_with_same_timestamp_collects_no_live_evidence(self):
        self.p.frigate.association_frame.side_effect = [None, self.anchor_image]
        self.run_frame()
        consumed = self.st['live_snapshot_consumed_ts']
        self.st['snapshot_anchor_retry_at'] = time.monotonic() - 1
        self.p._process('e')
        self.assertIsNotNone(self.st['snapshot_anchor'])
        self.assertEqual(self.st['live_snapshot_consumed_ts'], consumed)
        self.p.frigate.live_event_frame.assert_called_once()
        self.p._process_face.assert_not_called()

    def test_event_end_during_anchor_fetch_blocks_before_model_work(self):
        def fetch(*args):
            self.st['ended'] = True
            return self.anchor_image
        self.p.frigate.association_frame.side_effect = fetch
        self.run_frame()
        self.p._process_face.assert_not_called()
        self.p.engine.embed_face.assert_not_called()

    def test_source_change_during_anchor_fetch_does_not_admit_old_snapshot(self):
        def fetch(*args):
            self.profile['observation_source'] = 'restream'
            return self.anchor_image
        self.p.frigate.association_frame.side_effect = fetch
        self.run_frame()
        self.p._process_face.assert_not_called()

    def test_overcrowded_frame_never_chooses_four_arbitrary_faces(self):
        self.run_frame(faces=[face(box=(i * 40, 0, i * 40 + 30, 80)) for i in range(5)])
        self.p._process_face.assert_not_called()
        self.assertEqual(self.st['snapshot_association_reason'], 'association_overcrowded_frame')
        self.p.engine.embed_face.assert_not_called()
        self.p.frigate.association_frame.assert_not_called()

    def test_overlap_resets_confirmation_history(self):
        self.run_frame()
        self.st['liveness_history'] = [{'live': True}] * 2
        self.run_frame(faces=[face(), face((0, 1, 0), (40, 0, 120, 80))])
        self.assertEqual(self.p._process_face.call_count, 1)
        self.assertEqual(self.st['liveness_history'], [])
        self.assertEqual(self.st['snapshot_association_reason'], 'association_overlap_or_crossing')

    def test_crossing_resets_even_without_overlapping_current_boxes(self):
        self.run_frame(faces=[face(), face((0, 1, 0), (100, 0, 180, 80))])
        self.st['liveness_history'] = [{'live': True}] * 2
        self.run_frame(faces=[face(box=(100, 0, 180, 80)), face((0, 1, 0))])
        self.assertEqual(self.p._process_face.call_count, 1)
        self.assertEqual(self.st['liveness_history'], [])
        self.assertEqual(self.st['snapshot_association_reason'], 'association_overlap_or_crossing')

    def test_impossible_motion_creates_new_history_not_old_confirmations(self):
        self.run_frame()
        self.st['liveness_history'] = [{'live': True}] * 2
        self.run_frame(faces=[face(box=(1000, 0, 1080, 80))])
        self.assertEqual(self.st['liveness_history'], [])
        self.assertEqual(self.p._process_face.call_count, 2)

    def test_missing_subject_resets_history(self):
        self.run_frame()
        self.st['liveness_history'] = [{'live': True}] * 2
        self.run_frame(faces=[face((0, 1, 0))])
        self.assertEqual(self.st['liveness_history'], [])
        self.assertEqual(self.p._process_face.call_count, 1)

    def test_late_competing_anchor_invalidates_inflight_guard(self):
        def process(*args, **kwargs):
            guard = self.st['recognition_guard']
            self.assertTrue(guard())
            self.add_event('other', [1, 0, 0])
            self.assertFalse(guard())
        self.p._process_face.side_effect = process
        self.run_frame()
        self.assertEqual(self.st['liveness_history'], [])
        self.assertEqual(self.st['snapshot_association_reason'], 'association_changed_during_processing')

    def test_three_distinct_samples_required_after_anchor_with_real_confirmation(self):
        del self.p._process_face
        self.p.liveness = LivenessDetector(required_frames=2)
        self.p.liveness.analyze = Mock(return_value={'state': 'live', 'live': True, 'score': .9, 'face_px': 80})
        self.p.gallery = Mock()
        self.p.gallery.match_candidates.return_value = [('subject', 'Subject', .8), ('other', 'Other', .1)]
        self.p.gallery.match_ignored_detail.return_value = (0, None, None)
        self.p.access1.enqueue = Mock()
        self.p._publish_notification = Mock()
        self.p._publish_recognition = Mock()
        for number in (1, 2):
            self.run_frame(faces=[face((1, number / 100., 0))])
            self.p.access1.enqueue.assert_not_called()
        self.run_frame(faces=[face((1, .03, 0))])
        self.p.access1.enqueue.assert_called_once()
        self.assertEqual(self.p.access1.enqueue.call_args.args[0]['liveness_frames'], 3)
        self.assertEqual(self.p.access1.enqueue.call_args.args[0]['evidence_source'], 'secure_live_snapshot')
        self.p.frigate.association_frame.assert_called_once()


if __name__ == '__main__':
    unittest.main()
