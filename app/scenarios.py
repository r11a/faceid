"""Group adjacent Frigate events into one cross-camera visit."""
import json
import time
import uuid


class ScenarioManager:
    def __init__(self, audit, *, window_seconds: float = 90, camera_graph=None):
        self.audit = audit
        self.window_seconds = float(window_seconds)
        self.camera_graph = {
            camera: set(neighbors)
            for camera, neighbors in (camera_graph or {}).items()
        }

    def _adjacent(self, previous: set[str], camera: str) -> bool:
        if not self.camera_graph:
            return True
        return camera in previous or any(
            camera in self.camera_graph.get(item, set()) for item in previous
        )

    def attach(
        self, event_id: str, *, camera: str, start_ts: float, end_ts: float,
        status: str, person: str | None,
    ):
        candidates = self.audit.scenario_candidates(
            start_ts - self.window_seconds
        )
        selected = None
        for candidate in candidates:
            cameras = set(json.loads(candidate["cameras"]))
            if not self._adjacent(cameras, camera):
                continue
            # Time and camera adjacency alone are not identity evidence. Scenarios
            # continue only through an equal face identity or an explicit Re-ID hint.
            if not person or candidate.get("person") != person:
                continue
            selected = candidate
            break

        if selected is None:
            selected = {
                "scenario_id": f"s{int(start_ts)}-{uuid.uuid4().hex[:8]}",
                "start_ts": start_ts,
                "end_ts": end_ts,
                "status": status,
                "person": person,
                "cameras": json.dumps([camera]),
                "event_count": 1,
            }
        else:
            cameras = set(json.loads(selected["cameras"]))
            cameras.add(camera)
            selected.update(
                start_ts=min(float(selected["start_ts"]), start_ts),
                end_ts=max(float(selected["end_ts"]), end_ts),
                person=selected.get("person") or person,
                status=self._stronger_status(selected["status"], status),
                cameras=json.dumps(sorted(cameras)),
                event_count=int(selected["event_count"]) + 1,
            )

        self.audit.save_scenario(selected)
        self.audit.attach_scenario(event_id, selected["scenario_id"])
        selected["cameras"] = json.loads(selected["cameras"])
        selected["updated_ts"] = time.time()
        return selected

    @staticmethod
    def _stronger_status(left: str, right: str):
        rank = {
            "recognized": 5,
            "probable_reid": 4,
            "ambiguous": 3,
            "unknown": 2,
            "no_face": 1,
            "ignored": 0,
        }
        return max((left, right), key=lambda status: rank.get(status, 0))
