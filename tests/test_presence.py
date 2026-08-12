import unittest

from app.presence import RecognitionSessionTracker


class RecognitionSessionTrackerTests(unittest.TestCase):
    def test_continuous_presence_is_quiet_but_moves_and_returns_are_events(self):
        tracker = RecognitionSessionTracker(gap_seconds=300)
        self.assertEqual(tracker.classify("Alice", "front", 1000), "arrival")
        self.assertEqual(
            tracker.classify("Alice", "front", 1100), "presence_update"
        )
        self.assertEqual(
            tracker.classify("Alice", "office", 1150), "camera_transition"
        )
        self.assertEqual(tracker.classify("Alice", "office", 1501), "arrival")


if __name__ == "__main__":
    unittest.main()
