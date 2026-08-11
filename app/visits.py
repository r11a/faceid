"""Turn repeated recognition events into human-readable visits."""
import time


class VisitService:
    def __init__(self, audit, camera_profiles, gap_minutes: int = 15):
        self.audit = audit
        self.camera_profiles = camera_profiles
        self.gap_seconds = max(60, int(gap_minutes) * 60)

    def list(self, *, person: str | None = None, days: int = 30, limit: int = 200):
        rows = self.audit.recognized_timeline(
            person=person, after_ts=time.time() - max(1, min(days, 365)) * 86400
        )
        visits = []
        current = {}
        for event in rows:
            name = event["person"]
            previous = current.get(name)
            role = self.camera_profiles.get(event["camera"])["role"]
            starts_entry = role in {"entry", "entry_exit"}
            if (
                previous is None
                or float(event["start_ts"]) - float(previous["end_ts"]) > self.gap_seconds
                or previous["departure"] == "confirmed"
                or (
                    starts_entry and previous["last_camera"] != event["camera"]
                    and float(event["start_ts"]) - float(previous["end_ts"]) > 60
                )
            ):
                previous = {
                    "person": name,
                    "start_ts": float(event["start_ts"]),
                    "end_ts": float(event.get("end_ts") or event["start_ts"]),
                    "first_camera": event["camera"],
                    "last_camera": event["camera"],
                    "route": [], "events": [], "timeline": [], "scores": [],
                    "arrival": "confirmed" if starts_entry else "observed",
                    "departure": "not_observed",
                }
                current[name] = previous
                visits.append(previous)
            previous["end_ts"] = max(
                float(previous["end_ts"]),
                float(event.get("end_ts") or event["start_ts"]),
            )
            previous["last_camera"] = event["camera"]
            if not previous["route"] or previous["route"][-1] != event["camera"]:
                previous["route"].append(event["camera"])
            previous["events"].append(event["event_id"])
            previous["timeline"].append({
                "event_id": event["event_id"], "camera": event["camera"],
                "start_ts": float(event["start_ts"]),
            })
            previous["scores"].append(float(event.get("score") or 0))
            if role in {"exit", "entry_exit"}:
                previous["departure"] = "confirmed"

        now = time.time()
        for visit in visits:
            scores = visit.pop("scores")
            visit["event_count"] = len(visit["events"])
            visit["avg_score"] = round(sum(scores) / len(scores), 4) if scores else 0
            visit["duration_seconds"] = max(0, int(visit["end_ts"] - visit["start_ts"]))
            visit["open"] = (
                visit["departure"] != "confirmed"
                and now - visit["end_ts"] <= self.gap_seconds
            )
        visits.sort(key=lambda item: item["start_ts"], reverse=True)
        return visits[:max(1, min(limit, 1000))]

    def person_statistics(self, person: str, days: int = 30) -> dict:
        visits = self.list(person=person, days=days, limit=1000)
        durations = [v["duration_seconds"] for v in visits if v["event_count"] > 1]
        hours = [time.localtime(v["start_ts"]).tm_hour for v in visits]
        common_hour = max(set(hours), key=hours.count) if hours else None
        return {
            "visits": len(visits),
            "confirmed_arrivals": sum(v["arrival"] == "confirmed" for v in visits),
            "confirmed_departures": sum(v["departure"] == "confirmed" for v in visits),
            "average_duration_seconds": (
                round(sum(durations) / len(durations)) if durations else None
            ),
            "common_arrival_hour": common_hour,
        }
