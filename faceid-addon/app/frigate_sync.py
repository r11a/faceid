"""Operator-controlled reconciliation with Frigate's native face library."""
from __future__ import annotations

import hashlib
import io
import json
import logging
import mimetypes
import threading
import time
from pathlib import Path
from urllib.parse import quote

import cv2
import numpy as np

from .engine import crop_face
from .gallery import _atomic_write_json
from .quality import measure_face_quality

log = logging.getLogger("faceid.frigate_sync")


class FrigateGallerySync:
    """Never mutates either gallery without an explicit per-image selection."""

    def __init__(self, data_dir: Path, gallery, engine, frigate):
        self.data_dir = data_dir
        self.gallery = gallery
        self.engine = engine
        self.frigate = frigate
        self.ledger_path = data_dir / "frigate_sync.json"
        self._lock = threading.Lock()

    def _ledger(self) -> dict:
        try:
            value = json.loads(self.ledger_path.read_text("utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_ledger(self, value: dict):
        _atomic_write_json(self.ledger_path, value, indent=1)

    def remote_faces(self) -> dict[str, list[str]]:
        r = self.frigate.request("GET", "/api/faces", timeout=self.frigate.timeout * 3)
        if r.status_code != 200:
            raise RuntimeError(f"Frigate face library returned HTTP {r.status_code}")
        value = r.json()
        if not isinstance(value, dict):
            raise RuntimeError("Frigate returned an invalid face-library response")
        return {
            str(name): [str(file) for file in files]
            for name, files in value.items()
            if name != "train" and isinstance(files, list)
        }

    @staticmethod
    def _key(person: str, file: str) -> str:
        return f"{person}\0{file}"

    @staticmethod
    def _canonical_person(name: str) -> str:
        return " ".join(name.replace("_", " ").split()).casefold()

    def report(self) -> dict:
        remote = self.remote_faces()
        local = self.gallery.persons()
        ledger = self._ledger()
        exported = ledger.get("exported", {})
        imported = ledger.get("imported", {})
        local_rows, remote_rows = [], []
        remote_names = {self._canonical_person(name) for name in remote}
        for slug, person in local.items():
            pdir = self.gallery.persons_dir / slug
            for file in person.get("files", []):
                path = pdir / file
                try:
                    digest = hashlib.sha256(path.read_bytes()).hexdigest()
                except OSError:
                    continue
                local_rows.append({
                    "slug": slug, "person": person["name"], "file": file,
                    "url": f"media/persons/{quote(slug)}/{quote(file)}",
                    "exported": digest in exported,
                    "remote_person_exists": (
                        self._canonical_person(person["name"]) in remote_names
                    ),
                })
        for person, files in remote.items():
            for file in files:
                key = self._key(person, file)
                remote_rows.append({
                    "person": person, "file": file,
                    "url": (
                        "api/frigate-sync/image?person=" + quote(person, safe="")
                        + "&file=" + quote(file, safe="")
                    ),
                    "imported": key in imported,
                    "local_person_exists": any(
                        self._canonical_person(p["name"])
                        == self._canonical_person(person)
                        for p in local.values()
                    ),
                })
        return {
            "local": local_rows, "remote": remote_rows,
            "summary": {
                "local_people": len(local), "local_images": len(local_rows),
                "frigate_people": len(remote), "frigate_images": len(remote_rows),
                "export_candidates": sum(not row["exported"] for row in local_rows),
                "import_candidates": sum(not row["imported"] for row in remote_rows),
            },
        }

    def remote_image(self, person: str, file: str) -> bytes:
        if not person or not file or any(part in ("", ".", "..") for part in (person, file)):
            raise ValueError("unsafe image name")
        path = f"/clips/faces/{quote(person, safe='')}/{quote(file, safe='')}"
        r = self.frigate.request("GET", path, timeout=self.frigate.timeout * 4)
        if r.status_code != 200 or not r.content or len(r.content) > 12_000_000:
            raise RuntimeError(f"Frigate image returned HTTP {r.status_code}")
        return r.content

    def import_selected(self, items: list[dict]) -> dict:
        ok, skipped, errors = 0, 0, []
        with self._lock:
            ledger = self._ledger()
            imported = ledger.setdefault("imported", {})
            for item in items[:200]:
                person, file = str(item.get("person") or ""), str(item.get("file") or "")
                key = self._key(person, file)
                if key in imported:
                    skipped += 1
                    continue
                try:
                    raw = self.remote_image(person, file)
                    image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
                    if image is None:
                        raise RuntimeError("image could not be decoded")
                    face = self.engine.best_face(self.engine.faces(image), min_px=32)
                    if face is None:
                        raise RuntimeError("FaceID found no usable face")
                    quality = measure_face_quality(image, face, min_face_px=32, min_quality=0.2)
                    normalized = person.replace("_", " ").strip()
                    local = self.gallery.persons()
                    slug = next(
                        (s for s, p in local.items()
                         if self._canonical_person(p["name"])
                         == self._canonical_person(normalized)),
                        None,
                    ) or self.gallery.create_person(normalized)
                    self.gallery.add_face(
                        slug, crop_face(image, face.bbox), face.normed_embedding,
                        source={"source": "frigate-sync", "frigate_file": file,
                                "quality": quality.score},
                    )
                    imported[key] = {"ts": time.time(), "person": normalized}
                    ok += 1
                except Exception as exc:
                    errors.append({"person": person, "file": file, "error": str(exc)[:180]})
            self._save_ledger(ledger)
        return {"imported": ok, "skipped": skipped, "errors": errors}

    def export_selected(self, items: list[dict]) -> dict:
        ok, skipped, errors = 0, 0, []
        with self._lock:
            ledger = self._ledger()
            exported = ledger.setdefault("exported", {})
            remote_names = self.remote_faces()
            known_remote = {
                self._canonical_person(name): name for name in remote_names
            }
            people = self.gallery.persons()
            for item in items[:200]:
                slug, file = str(item.get("slug") or ""), str(item.get("file") or "")
                person = people.get(slug)
                path = self.gallery.persons_dir / slug / Path(file).name
                if person is None or file not in person.get("files", []) or not path.is_file():
                    errors.append({"slug": slug, "file": file, "error": "local image not found"})
                    continue
                raw = path.read_bytes()
                digest = hashlib.sha256(raw).hexdigest()
                if digest in exported:
                    skipped += 1
                    continue
                name = person["name"]
                canonical_name = self._canonical_person(name)
                remote_name = known_remote.get(canonical_name, name)
                encoded = quote(remote_name, safe="")
                try:
                    if canonical_name not in known_remote:
                        created = self.frigate.request("POST", f"/api/faces/{encoded}/create")
                        if created.status_code not in (200, 201):
                            raise RuntimeError(f"create returned HTTP {created.status_code}")
                        known_remote[canonical_name] = name
                    content_type = mimetypes.guess_type(file)[0] or "image/jpeg"
                    registered = self.frigate.request(
                        "POST", f"/api/faces/{encoded}/register",
                        files={"file": (Path(file).name, io.BytesIO(raw), content_type)},
                        timeout=self.frigate.timeout * 10,
                    )
                    if registered.status_code not in (200, 201):
                        detail = registered.text[:160]
                        raise RuntimeError(
                            f"register returned HTTP {registered.status_code}: {detail}"
                        )
                    exported[digest] = {"ts": time.time(), "person": name, "file": file}
                    ok += 1
                except Exception as exc:
                    errors.append({"person": name, "file": file, "error": str(exc)[:180]})
            self._save_ledger(ledger)
        return {"exported": ok, "skipped": skipped, "errors": errors}
