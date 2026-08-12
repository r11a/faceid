import unittest
from types import SimpleNamespace

import numpy as np

from app.enrollment import choose_enrollment_face


def face(vector, bbox):
    embedding = np.asarray(vector, dtype=np.float32)
    embedding /= np.linalg.norm(embedding)
    return SimpleNamespace(
        bbox=np.asarray(bbox, dtype=np.float32),
        normed_embedding=embedding,
    )


class EnrollmentSelectionTests(unittest.TestCase):
    def test_single_face_is_selected(self):
        candidate = face([1, 0], [0, 0, 100, 100])
        result = choose_enrollment_face([candidate])
        self.assertIs(result.face, candidate)
        self.assertEqual(result.reason, "single_face")

    def test_group_photo_requires_choice_without_references(self):
        result = choose_enrollment_face([
            face([1, 0], [0, 0, 100, 100]),
            face([0, 1], [120, 0, 220, 100]),
        ])
        self.assertIsNone(result.face)
        self.assertEqual(result.reason, "needs_selection")
        self.assertEqual([row["index"] for row in result.candidates], [0, 1])

    def test_clear_existing_identity_selects_target_not_largest_face(self):
        bystander = face([0, 1], [0, 0, 180, 180])
        target = face([1, 0], [200, 0, 290, 90])
        result = choose_enrollment_face(
            [bystander, target], np.asarray([[1.0, 0.0]], dtype=np.float32)
        )
        self.assertIs(result.face, target)
        self.assertEqual(result.index, 1)
        self.assertEqual(result.reason, "clear_gallery_match")

    def test_close_matches_are_not_guessed(self):
        first = face([1, 0], [0, 0, 100, 100])
        second = face([.99, .1], [120, 0, 220, 100])
        result = choose_enrollment_face(
            [first, second], np.asarray([[1.0, 0.0]], dtype=np.float32)
        )
        self.assertEqual(result.reason, "needs_selection")

    def test_explicit_choice_must_meet_minimum_size(self):
        too_small = face([1, 0], [0, 0, 40, 40])
        result = choose_enrollment_face([too_small], requested_index=0)
        self.assertEqual(result.reason, "invalid_selection")


if __name__ == "__main__":
    unittest.main()
