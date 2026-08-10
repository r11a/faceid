import io
import json
import tarfile
import tempfile
import unittest
import importlib.util
import sqlite3
from pathlib import Path

import cv2
import numpy as np

from app.backup_util import build_backup_gz
from app.body_recognition import BodyRecognitionService
from app.frame_distributor import FrameDistributor


class MediaStub:
    def __init__(self, root):
        self.root = Path(root)

    def _path(self, event_id):
        return self.root / f"{event_id}.mp4"

    def clip_path(self, event_id):
        return None


def test_shared_frames_are_loaded_from_bounded_cache(tmp_path):
    distributor = FrameDistributor(tmp_path, MediaStub(tmp_path), max_frames=5)
    target = distributor._dir("event-1")
    target.mkdir(parents=True)
    cv2.imwrite(str(target / "000001.jpg"), np.full((80, 120, 3), 120, np.uint8))
    rows = distributor.frames("event-1")
    assert len(rows) == 1
    assert distributor.report()["cache_hits"] == 1


def test_body_material_requires_review_and_consensus(tmp_path):
    service = BodyRecognitionService(tmp_path, enabled=False)
    image = np.zeros((300, 120, 3), np.uint8)
    for row in range(image.shape[0]):
        image[row, :, :] = row % 255
    added = service.add_pending("e1", image, "Ronen", "door")
    assert added["added"]
    assert service.materials()["pending"][0]["suggested_person"] == "Ronen"
    assert service.review(added["id"], "approve", "Ronen")
    assert service.materials()["approved"]["Ronen"] == 1
    assert service.status()["authority"] == "advisory-only"


def test_v3_backup_has_body_history_manifest_and_no_media_cache(tmp_path):
    (tmp_path / "persons" / "ronen").mkdir(parents=True)
    (tmp_path / "persons" / "ronen" / "face.jpg").write_bytes(b"face")
    (tmp_path / "body" / "approved" / "Ronen").mkdir(parents=True)
    (tmp_path / "body" / "approved" / "Ronen" / "body.jpg").write_bytes(b"body")
    (tmp_path / "body" / "model").mkdir()
    (tmp_path / "body" / "model" / "classifier.pkl").write_bytes(b"never-restore-executable")
    connection = sqlite3.connect(tmp_path / "audit.db")
    connection.execute("create table events(id text)")
    connection.execute("insert into events values ('e1')")
    connection.commit(); connection.close()
    (tmp_path / "media_cache").mkdir()
    (tmp_path / "media_cache" / "clip.mp4").write_bytes(b"secret-video")
    raw = build_backup_gz(tmp_path)
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        names = archive.getnames()
        manifest = json.load(archive.extractfile("manifest.json"))
    assert "body/approved/Ronen/body.jpg" in names
    assert "system/audit.db" in names
    assert "body/model/classifier.pkl" not in names
    assert all(not name.startswith("media_cache") for name in names)
    assert "frigate-credentials" in manifest["excludes"]


class AdvancedV3Tests(unittest.TestCase):
    def test_shared_frame_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            test_shared_frames_are_loaded_from_bounded_cache(Path(directory))

    def test_reviewed_body_material(self):
        with tempfile.TemporaryDirectory() as directory:
            test_body_material_requires_review_and_consensus(Path(directory))

    def test_full_backup_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            test_v3_backup_has_body_history_manifest_and_no_media_cache(Path(directory))

    @unittest.skipUnless(importlib.util.find_spec("sklearn"), "scikit-learn not installed")
    def test_body_training_is_atomic_and_armed(self):
        with tempfile.TemporaryDirectory() as directory:
            service = BodyRecognitionService(Path(directory), threshold=.6)
            for person, base in (("Ronen", 40), ("Moshe", 190)):
                target = service.approved / person; target.mkdir()
                for index in range(3):
                    cv2.imwrite(str(target / f"{index}.jpg"),
                                np.full((240, 100, 3), base + index * 5, np.uint8))
            service.embedding = lambda image: np.asarray(
                [float(image.mean()) / 255, 1 - float(image.mean()) / 255, .2],
                dtype="float32",
            )
            status = service.train()
            self.assertTrue(status["armed"])
            self.assertTrue((service.model_dir / "classifier.pkl").is_file())
