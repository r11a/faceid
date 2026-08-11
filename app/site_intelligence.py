"""Honest camera-zone map and anonymous traffic analytics."""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from collections import Counter
from pathlib import Path


class SiteIntelligence:
    def __init__(self, path: Path, audit, camera_profiles, visits):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.audit = audit
        self.camera_profiles = camera_profiles
        self.visits = visits
        self._lock = threading.RLock()

    def _read(self) -> dict:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _write(self, value: dict) -> None:
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2)
                handle.flush(); os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def map(self, cameras: list[str]) -> dict:
        stored = self._read()
        nodes = stored.get("cameras") if isinstance(stored.get("cameras"), dict) else {}
        count = max(1, len(cameras))
        result = []
        for index, camera in enumerate(cameras):
            saved = nodes.get(camera) if isinstance(nodes.get(camera), dict) else {}
            profile = self.camera_profiles.get(camera)
            result.append({
                "camera": camera, "label": saved.get("label") or camera,
                "x": float(saved.get("x", 10 + (index % 4) * 26)),
                "y": float(saved.get("y", 15 + (index // 4) * 28)),
                "role": profile.get("role", "observation"),
                "enabled": profile.get("enabled", True),
            })
        links = stored.get("links") if isinstance(stored.get("links"), list) else []
        return {"title": stored.get("title") or "האתר שלי", "cameras": result, "links": links,
                "notice": "המיקום והמסלול משוערים לפי מצלמות וזמנים — אינם GPS."}

    def update(self, body: dict, cameras: list[str]) -> dict:
        known = set(cameras)
        title = str(body.get("title") or "האתר שלי").strip()[:100]
        nodes = {}
        for item in body.get("cameras") or []:
            camera = str(item.get("camera") or "")
            if camera not in known: continue
            nodes[camera] = {"label": str(item.get("label") or camera)[:100],
                             "x": max(2, min(float(item.get("x", 50)), 98)),
                             "y": max(4, min(float(item.get("y", 50)), 96))}
        links, seen = [], set()
        for link in body.get("links") or []:
            if not isinstance(link, list) or len(link) != 2: continue
            a, b = str(link[0]), str(link[1])
            key = tuple(sorted((a, b)))
            if a in known and b in known and a != b and key not in seen:
                links.append([a, b]); seen.add(key)
        with self._lock: self._write({"title": title, "cameras": nodes, "links": links})
        return self.map(cameras)

    def analytics(self, *, cameras: list[str], days: int = 7) -> dict:
        days = max(1, min(int(days), 90))
        rows = self.audit.traffic_events(after_ts=time.time() - days * 86400)
        counts = Counter(row["camera"] for row in rows)
        hours = Counter(time.localtime(float(row.get("start_ts") or 0)).tm_hour for row in rows)
        transitions = Counter()
        # Identity is used only transiently to join events into visits; output is anonymous.
        for visit in self.visits.list(days=days, limit=1000):
            route = visit.get("route") or []
            for a, b in zip(route, route[1:]):
                if a != b: transitions[(a, b)] += 1
        peak = max(hours, key=hours.get) if hours else None
        total = sum(counts.values())
        return {
            "days": days, "total_person_events": total, "peak_hour": peak,
            "cameras": [{"camera": camera, "events": counts[camera],
                         "share": round(counts[camera] / total, 4) if total else 0}
                        for camera in cameras],
            "hours": [{"hour": hour, "events": hours[hour]} for hour in range(24)],
            "transitions": [{"from": a, "to": b, "count": count}
                            for (a, b), count in transitions.most_common(20)],
            "privacy": "Aggregates contain counts and routes only; no face image or identity is returned.",
        }
