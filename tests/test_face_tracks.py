import re
import unittest
from types import SimpleNamespace

import numpy as np

from app.face_tracks import FaceTracker


def face(embedding=(1., 0., 0.), x=0., y=0.):
    return SimpleNamespace(
        bbox=np.array([x, y, x + 100., y + 100.]),
        normed_embedding=np.asarray(embedding, dtype=float),
    )


class FaceTrackerTests(unittest.TestCase):
    def test_ids_are_unique_across_trackers_and_safe_for_event_paths(self):
        ids = [FaceTracker().update([face()], 0)[0][0][0] for _ in range(10)]
        self.assertEqual(len(set(ids)), 10)
        self.assertTrue(all(re.fullmatch(r'faceid-[0-9a-f]{32}', value) for value in ids))

    def test_detection_order_and_crossing_do_not_swap_people(self):
        tracker = FaceTracker()
        first, _ = tracker.update([face(x=0), face((0, 1, 0), x=180)], 0)
        second_faces = [face((0, 1, 0), x=80), face(x=100)]
        second, expired = tracker.update(second_faces, .5)
        self.assertEqual([item[0] for item in second], [first[1][0], first[0][0]])
        self.assertIs(second[0][1], second_faces[0])
        third, _ = tracker.update([face(x=180), face((0, 1, 0), x=0)], 1)
        self.assertEqual([item[0] for item in third], [first[0][0], first[1][0]])
        self.assertEqual(expired, [])

    def test_newcomer_in_same_position_gets_fresh_track(self):
        tracker = FaceTracker()
        first, _ = tracker.update([face()], 0)
        second, _ = tracker.update([face((0, 1, 0))], .1)
        self.assertNotEqual(first[0][0], second[0][0])

    def test_equal_candidates_cannot_inherit_either_track(self):
        tracker = FaceTracker()
        first, _ = tracker.update([face((.8, .6, 0)), face((.8, -.6, 0))], 0)
        second, _ = tracker.update([face()], .1)
        self.assertNotIn(second[0][0], [item[0] for item in first])

    def test_two_equal_detections_cannot_claim_one_existing_track(self):
        tracker = FaceTracker()
        first, _ = tracker.update([face()], 0)
        second, _ = tracker.update([face(), face()], .1)
        self.assertEqual(len(second), 2)
        self.assertEqual(len(set(item[0] for item in second)), 2)
        self.assertNotIn(first[0][0], [item[0] for item in second])

    def test_embedding_match_also_requires_plausible_position(self):
        tracker = FaceTracker()
        first, _ = tracker.update([face()], 0)
        second, _ = tracker.update([face(x=10000)], .1)
        self.assertNotEqual(first[0][0], second[0][0])

    def test_absence_expires_at_boundary_and_reentry_is_new(self):
        tracker = FaceTracker(absence_seconds=2)
        first, _ = tracker.update([face()], 0)
        self.assertEqual(tracker.update([], 1.9), ([], []))
        second, expired = tracker.update([face()], 2)
        self.assertEqual(expired, [first[0][0]])
        self.assertNotEqual(first[0][0], second[0][0])
        self.assertEqual(tracker.update([], 4), ([], [second[0][0]]))

    def test_slow_sampling_without_observed_absence_keeps_identity(self):
        tracker = FaceTracker(absence_seconds=2)
        first, _ = tracker.update([face()], 0)
        for now in [5, 30, 300]:
            current, expired = tracker.update([face()], now)
            self.assertEqual(current[0][0], first[0][0])
            self.assertEqual(expired, [])

    def test_short_observed_absence_can_rejoin_before_rearm(self):
        tracker = FaceTracker(absence_seconds=2)
        first, _ = tracker.update([face()], 0)
        self.assertEqual(tracker.update([], .5), ([], []))
        current, expired = tracker.update([face()], 1)
        self.assertEqual(current[0][0], first[0][0])
        self.assertEqual(expired, [])
        later, expired = tracker.update([face()], 5)
        self.assertEqual(later[0][0], first[0][0])
        self.assertEqual(expired, [])

    def test_long_observed_absence_retires_before_matching_reentry(self):
        tracker = FaceTracker(absence_seconds=2)
        first, _ = tracker.update([face()], 0)
        self.assertEqual(tracker.update([], .5), ([], []))
        current, expired = tracker.update([face()], 3)
        self.assertNotEqual(current[0][0], first[0][0])
        self.assertEqual(expired, [first[0][0]])

    def test_unmatched_old_track_expires_in_time_to_admit_newcomer_at_capacity(self):
        tracker = FaceTracker(max_tracks=1)
        first, _ = tracker.update([face()], 0)
        current, expired = tracker.update([face((0, 1, 0))], 5)
        self.assertEqual(len(current), 1)
        self.assertNotEqual(current[0][0], first[0][0])
        self.assertEqual(expired, [first[0][0]])

    def test_absence_setting_can_change_without_replacing_tracker(self):
        tracker = FaceTracker(absence_seconds=10)
        first, _ = tracker.update([face()], 0)
        tracker.absence_seconds = 1
        self.assertEqual(tracker.update([], 1), ([], [first[0][0]]))

    def test_clock_regression_does_not_rewind_last_seen(self):
        tracker = FaceTracker(absence_seconds=2)
        first, _ = tracker.update([face()], 10)
        backward, _ = tracker.update([face()], 3)
        self.assertEqual(backward[0][0], first[0][0])
        self.assertEqual(tracker.update([], 11), ([], []))
        self.assertEqual(tracker.update([], 12), ([], [first[0][0]]))

    def test_capacity_limits_history_and_does_not_evict_visible_track(self):
        tracker = FaceTracker(max_tracks=2)
        first, _ = tracker.update([face(), face((0, 1, 0)), face((0, 0, 1))], 0)
        self.assertEqual(len(first), 2)
        second, _ = tracker.update([face((0, 0, 1)), face((0, 1, 0))], .1)
        self.assertEqual([item[0] for item in second], [first[1][0]])
        for index in range(1, 50):
            result, expired = tracker.update([face()], index * 3)
            self.assertEqual(len(result), 1)
            self.assertLessEqual(len(expired), 2)
        self.assertEqual(len(tracker.update([], 150)[1]), 1)

    def test_invalid_embeddings_never_inherit_or_consume_track_capacity(self):
        tracker = FaceTracker(max_tracks=1)
        invalid = [face((0, 0, 0)), face((float('nan'), 0, 0)),
                   face((float('inf'), 0, 0)), face(())]
        missing = SimpleNamespace(bbox=np.array([0, 0, 100, 100]))
        first, _ = tracker.update(invalid + [missing, face()], 0)
        self.assertEqual(len(first), 1)
        self.assertEqual(tracker.update(invalid + [missing], .5), ([], []))
        self.assertEqual(tracker.update([], 2), ([], [first[0][0]]))

    def test_invalid_boxes_are_skipped(self):
        tracker = FaceTracker()
        invalid = face()
        invalid.bbox = np.array([0, 0, float('nan'), 100])
        inverted = face()
        inverted.bbox = np.array([100, 0, 0, 100])
        self.assertEqual(tracker.update([invalid, inverted], 0), ([], []))

    def test_different_embedding_shapes_do_not_match_or_crash(self):
        tracker = FaceTracker()
        first, _ = tracker.update([face()], 0)
        second, _ = tracker.update([face((1, 0, 0, 0))], .1)
        self.assertNotEqual(first[0][0], second[0][0])

    def test_touch_only_keeps_named_existing_tracks_alive(self):
        tracker = FaceTracker()
        first, _ = tracker.update([face(), face((0, 1, 0))], 10)
        tracker.touch([first[0][0], 'missing'], 11)
        tracker.touch([first[0][0]], 3)
        self.assertEqual(tracker.update([], 12), ([], [first[1][0]]))
        self.assertEqual(tracker.update([], 13), ([], [first[0][0]]))
        self.assertEqual(tracker.update([], 15), ([], []))

    def test_touch_clears_observed_absence_for_visible_track(self):
        tracker = FaceTracker()
        first, _ = tracker.update([face()], 0)
        self.assertEqual(tracker.update([], .5), ([], []))
        tracker.touch([first[0][0]], 1)
        current, expired = tracker.update([face()], 5)
        self.assertEqual(current[0][0], first[0][0])
        self.assertEqual(expired, [])


if __name__ == '__main__':
    unittest.main()
