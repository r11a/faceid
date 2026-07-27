import unittest

from app.calibration import UNKNOWN_LABEL, build_calibration_report


def event(event_id, truth, observations, camera="front"):
    return {
        "event": {
            "event_id": event_id, "ground_truth": truth, "camera": camera,
        },
        "observations": observations,
    }


class CalibrationTests(unittest.TestCase):
    def test_empty_calibration_does_not_invent_a_recommendation(self):
        report = build_calibration_report(
            [], current_threshold=.5, current_margin=.08, confirmations=2,
        )
        self.assertFalse(report["ready"])
        self.assertIsNone(report["recommended"])

    def test_calibration_is_event_level_and_counts_false_accepts(self):
        rows = [
            event("known", "Alice", [
                {"person": "Alice", "score": .7, "margin": .2},
                {"person": "Alice", "score": .72, "margin": .2},
            ]),
            event("stranger", UNKNOWN_LABEL, [
                {"person": "Alice", "score": .65, "margin": .2},
                {"person": "Alice", "score": .66, "margin": .2},
            ]),
        ]
        report = build_calibration_report(
            rows, current_threshold=.5, current_margin=.08,
            confirmations=2, target_far=.01,
        )
        self.assertEqual(report["current"]["events"], 2)
        self.assertEqual(report["current"]["false_accepts"], 1)
        self.assertEqual(report["current"]["tar"], 1.0)

    def test_duplicate_frame_does_not_become_a_separate_event(self):
        rows = [event("one", "Alice", [
            {"person": "Alice", "score": .7, "margin": .2},
            {"person": "Alice", "score": .7, "margin": .2},
        ])]
        report = build_calibration_report(
            rows, current_threshold=.5, current_margin=.08,
            confirmations=2,
        )
        self.assertEqual(report["current"]["events"], 1)

    def test_wrong_known_identity_is_not_diluted_into_far(self):
        rows = [event("known", "Alice", [
            {"person": "Bob", "score": .8, "margin": .2},
            {"person": "Bob", "score": .82, "margin": .2},
        ])]
        report = build_calibration_report(
            rows, current_threshold=.5, current_margin=.08,
            confirmations=2,
        )
        self.assertEqual(report["current"]["far"], 0.0)
        self.assertEqual(report["current"]["false_identification_rate"], 1.0)
