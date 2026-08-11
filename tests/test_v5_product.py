import tempfile
import unittest
import json
from pathlib import Path

import numpy as np

from app.access_control import AccessControl
from app.animals import AnimalService
from app.migrations import run_migrations


class FakeFrigate:
    def snapshot(self, event_id, crop=True):
        return np.full((80, 120, 3), 127, dtype=np.uint8)


class ProductV5Tests(unittest.TestCase):
    def test_migration_is_backed_up_and_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "settings.json").write_text("{}", encoding="utf-8")
            first = run_migrations(root)
            self.assertTrue(first["changed"])
            self.assertTrue(Path(first["backup"]).is_file())
            second = run_migrations(root)
            self.assertFalse(second["changed"])
            self.assertEqual(second["to"], 5)

    def test_access_policy_is_safe_by_default_and_role_ready(self):
        with tempfile.TemporaryDirectory() as folder:
            service = AccessControl(Path(folder) / "access.json", enabled=False)
            session = service.session({"x-remote-user-id": "ronen", "x-remote-user-name": "Ronen"})
            self.assertEqual(session["role"], "admin")
            self.assertFalse(session["enforced"])
            self.assertTrue(service.allowed({}, path="/api/settings", method="POST"))

            service.path.write_text(json.dumps({"assignments": {"ronen": "viewer"}}), encoding="utf-8")
            service.enabled = True
            viewer = service.session({"x-remote-user-id": "ronen"})
            self.assertEqual(viewer["role"], "viewer")
            self.assertNotIn("settings", viewer["tabs"])
            self.assertFalse(service.allowed(
                {"x-remote-user-id": "ronen"}, path="/api/persons/alice", method="DELETE"
            ))

    def test_pet_profile_and_frigate_animal_event(self):
        with tempfile.TemporaryDirectory() as folder:
            service = AnimalService(Path(folder), FakeFrigate())
            pet = service.save_profile(name="Luna", species="cat", frigate_name="luna")
            event = service.handle_event({
                "id": "cat-1", "label": "cat", "sub_label": "luna",
                "camera": "garden", "start_time": 1000, "has_snapshot": True,
            }, "end")
            self.assertEqual(event["pet_slug"], pet["slug"])
            self.assertEqual(event["name"], "Luna")
            self.assertTrue((Path(folder) / "animals" / "images" / "cat-1.jpg").is_file())
            summary = service.summary()["profiles"][pet["slug"]]
            self.assertEqual(summary["appearances"], 1)
            self.assertEqual(summary["last_camera"], "garden")

    def test_animal_event_id_cannot_escape_image_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            service = AnimalService(root, FakeFrigate())
            event = service.handle_event({
                "id": "../../outside", "label": "dog", "camera": "yard",
                "start_time": 1000, "has_snapshot": True,
            }, "end")
            self.assertTrue(event["image"].startswith("media/animals/"))
            self.assertFalse((root / "outside.jpg").exists())
            self.assertEqual(len(list((root / "animals" / "images").glob("*.jpg"))), 1)


if __name__ == "__main__":
    unittest.main()
