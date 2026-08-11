"""Frigate animal event ledger and simple named-pet profiles."""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from pathlib import Path

import cv2


ANIMAL_LABELS = {
    "dog", "cat", "bird", "horse", "sheep", "cow", "bear", "deer",
    "raccoon", "fox", "squirrel", "goat", "rabbit", "skunk", "possum",
    "rodent",
}


def _slug(value: str) -> str:
    result = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return result or f"pet-{uuid.uuid4().hex[:8]}"


def _event_filename(event_id: str) -> str:
    """Keep Frigate-controlled IDs inside the images directory."""
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(event_id)).strip("._")
    return f"{safe[:160] or uuid.uuid4().hex}.jpg"


class AnimalService:
    def __init__(self, data_dir: Path, frigate, *, retention_days: int = 30):
        self.root = data_dir / "animals"
        self.root.mkdir(parents=True, exist_ok=True)
        self.profiles_path = self.root / "profiles.json"
        self.events_path = self.root / "events.json"
        self.images = self.root / "images"
        self.images.mkdir(exist_ok=True)
        self.frigate = frigate
        self.retention_days = max(1, int(retention_days))
        self._lock = threading.RLock()
        self._seen: set[str] = set()
        self.prune()

    @staticmethod
    def _read(path: Path, default):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value
        except (OSError, json.JSONDecodeError):
            return default

    @staticmethod
    def _write(path: Path, value) -> None:
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2)
                handle.flush(); os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def profiles(self) -> dict:
        with self._lock:
            value = self._read(self.profiles_path, {})
            return value if isinstance(value, dict) else {}

    def save_profile(self, *, name: str, species: str, frigate_name: str = "", slug: str = "") -> dict:
        name = str(name).strip()
        species = str(species).strip().lower()
        if not name:
            raise ValueError("name is required")
        if species not in ANIMAL_LABELS:
            raise ValueError("unsupported animal species")
        with self._lock:
            profiles = self.profiles()
            key = slug if slug in profiles else _slug(name)
            profiles[key] = {
                "slug": key, "name": name, "species": species,
                "frigate_name": str(frigate_name).strip(),
                "updated_ts": time.time(),
            }
            self._write(self.profiles_path, profiles)
            return profiles[key]

    def delete_profile(self, slug: str) -> bool:
        with self._lock:
            profiles = self.profiles()
            if slug not in profiles:
                return False
            profiles.pop(slug)
            self._write(self.profiles_path, profiles)
            return True

    @staticmethod
    def _identity(after: dict) -> str:
        sub = after.get("sub_label")
        if isinstance(sub, (list, tuple)):
            sub = sub[0] if sub else ""
        if isinstance(sub, dict):
            sub = sub.get("label") or sub.get("name") or ""
        if sub:
            return str(sub)
        data = after.get("data") or {}
        for key, value in data.items():
            if key.endswith("_name") or key in {"pet", "classification"}:
                if isinstance(value, str) and value:
                    return value
        return ""

    def _matched_profile(self, species: str, identity: str) -> dict | None:
        identity_key = identity.casefold()
        for profile in self.profiles().values():
            if profile["species"] != species:
                continue
            configured = str(profile.get("frigate_name") or "").casefold()
            if configured and configured == identity_key:
                return profile
        return None

    def handle_event(self, after: dict, event_type: str, *, client=None, prefix="faceid") -> dict | None:
        species = str(after.get("label") or "").lower()
        event_id = str(after.get("id") or "")
        if species not in ANIMAL_LABELS or not event_id:
            return None
        if event_type not in {"new", "update", "end"}:
            return None
        if event_id in self._seen and event_type != "end":
            return None
        identity = self._identity(after)
        profile = self._matched_profile(species, identity)
        ts = float(after.get("start_time") or time.time())
        row = {
            "event_id": event_id, "species": species,
            "name": profile["name"] if profile else identity or None,
            "pet_slug": profile["slug"] if profile else None,
            "camera": str(after.get("camera") or ""), "start_ts": ts,
            "end_ts": after.get("end_time"), "score": float(after.get("score") or 0),
            "source": "frigate", "image": None,
        }
        with self._lock:
            events = self._read(self.events_path, [])
            events = [item for item in events if item.get("event_id") != event_id]
            image_name = _event_filename(event_id)
            existing_image = self.images / image_name
            if existing_image.is_file():
                row["image"] = f"media/animals/{image_name}"
            elif after.get("has_snapshot"):
                image = self.frigate.snapshot(event_id, crop=True)
                if image is not None and cv2.imwrite(str(existing_image), image):
                    row["image"] = f"media/animals/{image_name}"
            events.insert(0, row)
            self._write(self.events_path, events[:5000])
            self._seen.add(event_id)
        payload = {"schema_version": 1, "decision": "animal", **row}
        if client:
            client.publish(f"{prefix}/v1/animals", json.dumps(payload, ensure_ascii=False))
            key = profile["slug"] if profile else species
            client.publish(f"{prefix}/animal/{key}/state", row["camera"] or "seen", retain=True)
            client.publish(f"{prefix}/animal/{key}/attributes", json.dumps(row, ensure_ascii=False), retain=True)
            self.publish_discovery(client, prefix)
        return row

    def events(self, *, species: str = "", pet_slug: str = "", limit: int = 200) -> list[dict]:
        rows = self._read(self.events_path, [])
        if species:
            rows = [row for row in rows if row.get("species") == species]
        if pet_slug:
            rows = [row for row in rows if row.get("pet_slug") == pet_slug]
        return rows[:max(1, min(int(limit), 1000))]

    def summary(self) -> dict:
        rows = self.events(limit=5000)
        profiles = self.profiles()
        per_pet = {}
        for slug, profile in profiles.items():
            found = [row for row in rows if row.get("pet_slug") == slug]
            cameras = {}
            for row in found:
                cameras[row["camera"]] = cameras.get(row["camera"], 0) + 1
            per_pet[slug] = {
                **profile, "appearances": len(found),
                "last_seen": found[0]["start_ts"] if found else None,
                "last_camera": found[0]["camera"] if found else None,
                "top_camera": max(cameras, key=cameras.get) if cameras else None,
            }
        return {"profiles": per_pet, "events": rows[:100], "supported": sorted(ANIMAL_LABELS)}

    def publish_discovery(self, client, prefix: str) -> None:
        for slug, profile in self.profiles().items():
            config = {
                "name": f"{profile['name']} last location",
                "unique_id": f"{prefix}_pet_{slug}_location",
                "object_id": f"{prefix}_pet_{slug}_location",
                "state_topic": f"{prefix}/animal/{slug}/state",
                "json_attributes_topic": f"{prefix}/animal/{slug}/attributes",
                "icon": "mdi:paw",
                "device": {"identifiers": [f"{prefix}_animals"], "name": "FaceID Animals"},
            }
            client.publish(
                f"homeassistant/sensor/{prefix}_pet_{slug}/config",
                json.dumps(config, ensure_ascii=False), retain=True,
            )

    def prune(self) -> int:
        cutoff = time.time() - self.retention_days * 86400
        with self._lock:
            rows = self._read(self.events_path, [])
            keep, removed = [], 0
            for row in rows:
                if float(row.get("start_ts") or 0) >= cutoff:
                    keep.append(row)
                else:
                    image = row.get("image") or ""
                    if image:
                        (self.images / Path(image).name).unlink(missing_ok=True)
                    else:
                        (self.images / _event_filename(row.get("event_id") or "")).unlink(missing_ok=True)
                    removed += 1
            if removed:
                self._write(self.events_path, keep)
            return removed
