import tempfile
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
