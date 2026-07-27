"""Event-level calibration: no frame leakage, explicit FAR/FRR trade-offs."""
from collections import defaultdict


UNKNOWN_LABEL = "__unknown__"


def _predict(observations, threshold: float, margin: float, confirmations: int):
    candidates = defaultdict(list)
    for observation in observations:
        person = observation.get("person")
        if (
            person
            and float(observation.get("score") or 0.0) >= threshold
            and float(observation.get("margin") or 0.0) >= margin
            and observation.get("status") != "duplicate"
        ):
            candidates[person].append(observation)
    eligible = [
        (len(items), max(float(item.get("score") or 0.0) for item in items), person)
        for person, items in candidates.items()
        if len(items) >= confirmations
    ]
    return max(eligible)[2] if eligible else None


def _metrics(rows, threshold: float, margin: float, confirmations: int):
    known = unknown = correct = false_accept = false_reject = rejected_unknown = 0
    false_identification = 0
    per_camera = defaultdict(lambda: {
        "events": 0, "correct": 0, "false_accept": 0,
        "false_identification": 0,
    })
    per_person = defaultdict(lambda: {
        "events": 0, "correct": 0, "false_reject": 0,
        "false_identification": 0,
    })
    for row in rows:
        event = row["event"]
        truth = event["ground_truth"]
        predicted = _predict(row["observations"], threshold, margin, confirmations)
        camera = event["camera"]
        per_camera[camera]["events"] += 1
        if truth == UNKNOWN_LABEL:
            unknown += 1
            if predicted is None:
                rejected_unknown += 1
            else:
                false_accept += 1
                per_camera[camera]["false_accept"] += 1
        else:
            known += 1
            per_person[truth]["events"] += 1
            if predicted == truth:
                correct += 1
                per_camera[camera]["correct"] += 1
                per_person[truth]["correct"] += 1
            else:
                false_reject += 1
                per_person[truth]["false_reject"] += 1
                if predicted is not None:
                    false_identification += 1
                    per_camera[camera]["false_identification"] += 1
                    per_person[truth]["false_identification"] += 1
    return {
        "threshold": round(threshold, 3),
        "margin": round(margin, 3),
        "events": known + unknown,
        "known_events": known,
        "unknown_events": unknown,
        "tar": round(correct / max(1, known), 4),
        "frr": round(false_reject / max(1, known), 4),
        # FAR is an impostor-trial metric. Dividing by all events makes a busy
        # household appear safer without improving stranger rejection.
        "far": round(false_accept / max(1, unknown), 4),
        "false_identification_rate": round(
            false_identification / max(1, known), 4
        ),
        "unknown_rejection": round(rejected_unknown / max(1, unknown), 4),
        "false_accepts": false_accept,
        "false_rejects": false_reject,
        "false_identifications": false_identification,
        "per_camera": dict(per_camera),
        "per_person": dict(per_person),
    }


def build_calibration_report(
    rows, *, current_threshold: float, current_margin: float,
    confirmations: int, target_far: float = 0.01,
):
    current = _metrics(rows, current_threshold, current_margin, confirmations)
    candidates = []
    for step in range(20, 81, 2):
        threshold = step / 100.0
        for margin_step in range(0, 21, 2):
            candidates.append(
                _metrics(rows, threshold, margin_step / 100.0, confirmations)
            )
    safe = [item for item in candidates if item["far"] <= target_far]
    recommended = max(
        safe or candidates,
        key=lambda item: (
            item["tar"],
            item["unknown_rejection"],
            -item["far"],
            item["threshold"],
            item["margin"],
        ),
    ) if candidates else current
    return {
        "ready": len(rows) >= 20 and current["known_events"] >= 5
                 and current["unknown_events"] >= 5,
        "sample_warning": (
            None if len(rows) >= 20
            else "Label at least 20 independent events before trusting calibration."
        ),
        "target_far": target_far,
        "current": current,
        "recommended": recommended,
    }
