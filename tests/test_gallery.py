import tempfile
import time
import unittest
from pathlib import Path

try:
    import numpy as np
    from app.gallery import Gallery
except ModuleNotFoundError:
    np = None
    Gallery = None


@unittest.skipIf(Gallery is None, "gallery dependencies are not installed")
class GalleryTests(unittest.TestCase):
    def test_review_queue_is_bounded_and_keeps_representatives(self):
        with tempfile.TemporaryDirectory() as tmp:
            gallery = Gallery(Path(tmp), top_k=1)
            gallery.review_queue_max_total = 5
            gallery.review_queue_max_per_cluster = 3
            gallery.review_queue_retention_days = 14
            crop = np.zeros((64, 64, 3), dtype=np.uint8)
            embedding = np.zeros(512, dtype=np.float32)
            embedding[0] = 1.0
            for index in range(8):
                gallery.save_unknown(
                    crop, embedding,
                    {"event_id": f"event-{index}", "guess": "Alice"},
                    dedupe_sim=1.1,
                )
                time.sleep(.002)

            self.assertEqual(len(gallery.unknowns()), 3)
            result = gallery.prune_unknown_queue()
            self.assertEqual(result["remaining"], 3)
            self.assertEqual(result["policy"]["max_per_identity"], 3)

    def test_returns_runner_up_and_deletes_nested_trimmed_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            gallery = Gallery(Path(tmp), top_k=1)
            alice = gallery.create_person("Alice")
            bob = gallery.create_person("Bob")
            gallery._cache[alice]["emb"] = np.array([[1.0, 0.0]], dtype=np.float32)
            gallery._cache[alice]["files"] = ["alice.jpg"]
            gallery._cache[bob]["emb"] = np.array([[0.0, 1.0]], dtype=np.float32)
            gallery._cache[bob]["files"] = ["bob.jpg"]
            gallery._persist(alice)
            gallery._persist(bob)

            self.assertTrue(gallery.rename_person(alice, "Alice Smith"))
            self.assertEqual(gallery.persons()[alice]["name"], "Alice Smith")
            with self.assertRaises(ValueError):
                gallery.rename_person(alice, "Bob")

            candidates = gallery.match_candidates(
                np.array([0.8, 0.6], dtype=np.float32), limit=2
            )
            self.assertEqual([item[0] for item in candidates], [alice, bob])
            self.assertAlmostEqual(candidates[0][2], 0.8, places=5)
            self.assertAlmostEqual(candidates[1][2], 0.6, places=5)

            trimmed = Path(tmp) / "persons" / alice / "_trimmed"
            trimmed.mkdir()
            (trimmed / "old.jpg").write_bytes(b"old")
            gallery.delete_person(alice)
            self.assertFalse((Path(tmp) / "persons" / alice).exists())


if __name__ == "__main__":
    unittest.main()
