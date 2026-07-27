import unittest

from app.decision import DecisionAccumulator, DecisionPolicy


class DecisionAccumulatorTests(unittest.TestCase):
    def setUp(self):
        self.policy = DecisionPolicy(
            match_threshold=0.50,
            unknown_threshold=0.35,
            match_margin=0.08,
            ignore_threshold=0.50,
            ignore_margin=0.12,
            min_confirmations=2,
        )

    def add(self, acc, key, **overrides):
        values = {
            "slug": "alice",
            "person": "Alice",
            "score": 0.62,
            "runner_up": "Bob",
            "runner_up_score": 0.30,
            "ignore_score": 0.10,
            "ignore_group": None,
        }
        values.update(overrides)
        return acc.add(key=key, **values)

    def test_requires_two_distinct_confirmations(self):
        acc = DecisionAccumulator(self.policy)
        self.assertEqual(self.add(acc, "frame-1").status, "recognized_candidate")
        decision = self.add(acc, "frame-2")
        self.assertEqual(decision.status, "recognized")
        self.assertEqual(decision.person, "Alice")
        self.assertEqual(decision.confirmations, 2)

    def test_duplicate_frame_does_not_confirm(self):
        acc = DecisionAccumulator(self.policy)
        self.add(acc, "same-frame")
        decision = self.add(acc, "same-frame")
        self.assertEqual(decision.status, "duplicate")
        self.assertEqual(len(acc.observations), 1)

    def test_small_runner_up_margin_is_ambiguous(self):
        acc = DecisionAccumulator(self.policy)
        decision = self.add(
            acc, "frame-1", score=0.62, runner_up_score=0.58
        )
        self.assertEqual(decision.status, "ambiguous")

    def test_conflicting_people_do_not_form_consensus(self):
        acc = DecisionAccumulator(self.policy)
        self.add(acc, "frame-1")
        decision = self.add(
            acc, "frame-2", slug="bob", person="Bob", score=0.64,
            runner_up="Alice", runner_up_score=0.30,
        )
        self.assertEqual(decision.status, "recognized_candidate")
        self.assertEqual(acc.pending_status(), "ambiguous")

    def test_unknown_threshold_is_enforced(self):
        acc = DecisionAccumulator(self.policy)
        decision = self.add(
            acc, "frame-1", slug=None, person=None, score=0.20,
            runner_up=None, runner_up_score=0.0,
        )
        self.assertEqual(decision.status, "unknown")

    def test_ignore_requires_margin_and_consensus(self):
        acc = DecisionAccumulator(self.policy)
        first = self.add(
            acc, "frame-1", score=0.40, ignore_score=0.61,
            ignore_group="visitor-1",
        )
        self.assertEqual(first.status, "ignored_candidate")
        second = self.add(
            acc, "frame-2", score=0.39, ignore_score=0.63,
            ignore_group="visitor-1",
        )
        self.assertEqual(second.status, "ignored")
        self.assertEqual(second.confirmations, 2)


if __name__ == "__main__":
    unittest.main()
