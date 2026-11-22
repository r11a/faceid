"""Summarize one or more exported FaceID recognition captures for the same camera."""
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
from statistics import median, quantiles


def timing_stats(values):
    return {
        "count": len(values),
        "p50_ms": round(median(values), 1) if values else None,
        "p95_ms": round(quantiles(values, n=20, method="inclusive")[18], 1)
        if len(values) > 1 else (values[0] if values else None),
    }


def summarize(reports, camera):
    groups = {}
    for report in reports:
        for event in report.get("events", []):
            if event.get("camera") != camera:
                continue
            records = event.get("records", [])
            trigger = next((record for record in records if record.get("stage") == "stream_trigger_received"), None)
            if trigger is None or event.get("started_before_capture"):
                continue
            backend = (trigger.get("active_backend") or "unreported", trigger.get("native_device") or "unreported")
            group = groups.setdefault(backend, {"events": set(), "metrics": defaultdict(list), "reasons": Counter(),
                                              "recognition_complete": 0, "three_samples": 0})
            if event.get("event_id") in group["events"]:
                continue
            group["events"].add(event.get("event_id"))
            first_samples = set()
            completed = False
            for record in records:
                stage = record.get("stage")
                if (stage in ("stream_evidence_reset", "stream_finished", "stream_frame_rejected")
                        or (stage == "stream_frame" and record.get("reason") == "buffering_growth")):
                    group["reasons"][record.get("reason") or stage] += 1
                if stage == "stream_recognition_complete":
                    completed = True
                if stage == "stream_qualifying_sample":
                    count = record.get("qualifying_samples")
                    elapsed = record.get("stage_elapsed_ms")
                    if count in (1, 2, 3) and count not in first_samples and isinstance(elapsed, (float, int)):
                        group["metrics"][f"trigger_to_sample_{count}"].append(elapsed)
                        first_samples.add(count)
                for field in ("anchor_fetch_wait_ms", "anchor_model_ms", "detection_compute_ms"):
                    value = record.get(field)
                    if isinstance(value, (int, float)):
                        group["metrics"][field].append(value)
                elapsed = record.get("duration_ms")
                if stage in ("embedding_compute", "embedding_scheduler_wait", "liveness_inference") and isinstance(elapsed, (float, int)):
                    group["metrics"][stage].append(elapsed)
            group["recognition_complete"] += int(completed)
            group["three_samples"] += int(3 in first_samples)
    return [
        {"backend": backend, "device": device, "events": len(group["events"]),
         "events_with_three_samples": group["three_samples"],
         "recognition_complete": group["recognition_complete"], "reason_counts": dict(group["reasons"]),
         "timings": {key: timing_stats(values) for key, values in sorted(group["metrics"].items())}}
        for (backend, device), group in sorted(groups.items())
    ]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("reports", type=Path, nargs="+")
    parser.add_argument("--camera", default="Enterance")
    args = parser.parse_args()
    print(json.dumps(summarize([json.loads(path.read_text(encoding="utf-8-sig"))
                               for path in args.reports], args.camera), indent=2))
