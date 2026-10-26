import copy
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from app import gallery_coach


class Gallery:
    def __init__(self, root):
        self.persons_dir = Path(root)
        self.person = {"name": "Alice", "files": ["one.jpg", "two.jpg"], "sources": {}}
        directory = self.persons_dir / "alice"
        directory.mkdir()
        for file in self.person["files"]:
            cv2.imwrite(str(directory / file), np.full((24, 24), 128, dtype=np.uint8))
        np.save(directory / "embeddings.npy", np.eye(2, dtype=np.float32))

    def persons(self):
        return {"alice": copy.deepcopy(self.person)}


class GalleryCoachCacheTests(unittest.TestCase):
    def test_polling_reuses_work_and_returns_independent_reports(self):
        with tempfile.TemporaryDirectory() as root:
            gallery = Gallery(root)
            with patch.object(gallery_coach, "_image_metrics", wraps=gallery_coach._image_metrics) as metrics, patch.object(gallery_coach.np, "load", wraps=np.load) as load:
                original = gallery_coach.gallery_coach_report(gallery)
                expected = copy.deepcopy(original)
                original["people"][0]["images"][0]["reasons"].append("changed")
                self.assertEqual(gallery_coach.gallery_coach_report(gallery), expected)
                self.assertEqual(metrics.call_count, 2)
                self.assertEqual(load.call_count, 1)

    def test_changes_invalidate_immediately(self):
        with tempfile.TemporaryDirectory() as root:
            gallery = Gallery(root)
            directory = gallery.persons_dir / "alice"
            with patch.object(gallery_coach, "_image_metrics", wraps=gallery_coach._image_metrics) as metrics:
                gallery_coach.gallery_coach_report(gallery)
                cv2.imwrite(str(directory / "one.jpg"), np.zeros((32, 32), dtype=np.uint8))
                report = gallery_coach.gallery_coach_report(gallery)
                self.assertEqual(report["people"][0]["images"][0]["illumination"], 0)
                np.save(directory / "embeddings.npy", np.ones((2, 2), dtype=np.float32))
                report = gallery_coach.gallery_coach_report(gallery)
                self.assertEqual(report["people"][0]["images"][0]["novelty"], 0)
                gallery.person["sources"] = {"one.jpg": {"camera": "front"}}
                self.assertEqual(gallery_coach.gallery_coach_report(gallery)["people"][0]["cameras"], ["front"])
                gallery.person["name"] = "Renamed"
                self.assertEqual(gallery_coach.gallery_coach_report(gallery)["people"][0]["person"], "Renamed")
                gallery.person["files"].pop()
                self.assertEqual(gallery_coach.gallery_coach_report(gallery)["summary"]["images"], 1)
                (directory / "one.jpg").unlink()
                self.assertEqual(gallery_coach.gallery_coach_report(gallery)["people"][0]["images"][0]["sharpness"], 0)
                self.assertEqual(metrics.call_count, 12)

    def test_same_size_replacement_with_preserved_mtime_invalidates(self):
        with tempfile.TemporaryDirectory() as root:
            gallery = Gallery(root)
            path = gallery.persons_dir / "alice" / "embeddings.npy"
            before = gallery_coach.gallery_coach_report(gallery)
            stat = path.stat()
            replacement = path.with_suffix(".new.npy")
            np.save(replacement, np.ones((2, 2), dtype=np.float32))
            os.utime(replacement, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            os.replace(replacement, path)
            after = gallery_coach.gallery_coach_report(gallery)
            self.assertNotEqual(before, after)

    def test_cache_is_per_gallery_and_serializes_concurrent_polling(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as other:
            gallery, second = Gallery(root), Gallery(other)
            with patch.object(gallery_coach, "_image_metrics", wraps=gallery_coach._image_metrics) as metrics:
                with ThreadPoolExecutor(max_workers=4) as executor:
                    reports = list(executor.map(lambda _: gallery_coach.gallery_coach_report(gallery), range(8)))
                self.assertTrue(all(report == reports[0] for report in reports))
                self.assertEqual(metrics.call_count, 2)
                gallery_coach.gallery_coach_report(second)
                self.assertEqual(metrics.call_count, 4)

    def test_change_during_audit_is_not_cached(self):
        with tempfile.TemporaryDirectory() as root:
            gallery = Gallery(root)
            metrics = gallery_coach._image_metrics

            def changing_metrics(path):
                gallery.person["name"] = "Renamed during audit"
                return metrics(path)

            with patch.object(gallery_coach, "_image_metrics", side_effect=changing_metrics):
                gallery_coach.gallery_coach_report(gallery)
            self.assertIsNone(gallery._coach_report_cache)
            self.assertEqual(gallery_coach.gallery_coach_report(gallery)["people"][0]["person"], "Renamed during audit")


if __name__ == "__main__":
    unittest.main()
