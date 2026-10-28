"""Bounded camera sampling and independent face sightings using existing decisions."""
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import hashlib
import logging
import threading
import time

import cv2
import numpy as np

from .decision import DecisionAccumulator
from .engine import crop_face
from .face_tracks import FaceTracker
from .frigate_api import camera_frame_bytes
from .quality import measure_face_quality

log = logging.getLogger("faceid.continuous")


class ContinuousRecognition:
    def __init__(self, processor):
        self.processor = processor
        # Lazy threads; no camera tasks at all for the default event-only setup.
        self.executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="faceid-camera")
        self.lock = threading.RLock()
        self.cameras = {}
        self.pending = set()
        self.last_started = {}
        self.active_events = {}
        self.counts = {}
        self.activity_version = 0
        self.camera_versions = {}
        self.online = True
        self.reconciling = False
        self.next_reconcile = 0.0
        self.status = {}
        self.syncing_streams = False
        self.next_stream_sync = 0.0

    def profiles(self):
        profiles = self.processor.camera_profiles
        return {camera: profiles.get(camera) for camera in profiles.all()} if profiles else {}

    def reconnect(self):
        with self.lock:
            self.counts.clear()
            self.active_events.clear()
            self.activity_version += 1
            self.online = True
            self.next_reconcile = 0

    def mqtt(self, topic, raw):
        """Consume Frigate availability/counts before the event JSON decoder."""
        prefix = self.processor.frigate_topic + "/"
        if topic == prefix + "available":
            with self.lock:
                self.online = raw.decode(errors="replace") == "online"
                if not self.online:
                    self.counts.clear()
                    self.active_events.clear()
                self.activity_version += 1
                self.next_reconcile = 0
            return True
        if not topic.startswith(prefix):
            return False
        pieces = topic[len(prefix):].split("/")
        if len(pieces) != 2 or pieces[1] != "person":
            return False
        camera = pieces[0]
        if camera not in self.profiles():
            return True  # Zone counts must never activate a different camera.
        try:
            count = max(0, int(raw))
        except (ValueError, TypeError):
            return True
        with self.lock:
            self.counts[camera] = count
            if count == 0:
                self.active_events = {eid: cam for eid, cam in self.active_events.items() if cam != camera}
            self.camera_versions[camera] = self.camera_versions.get(camera, 0) + 1
        return True

    def observe_event(self, after, kind):
        if after.get("label") != "person" or not after.get("id") or not after.get("camera"):
            return
        if after["camera"] not in self.profiles():
            return
        with self.lock:
            camera = after["camera"]
            if kind == "end" or after.get("end_time"):
                self.active_events.pop(after["id"], None)
            else:
                self.active_events[after["id"]] = camera
            # A new object message supersedes the previous occupancy count.
            self.counts.pop(camera, None)
            self.camera_versions[camera] = self.camera_versions.get(camera, 0) + 1

    def eligible(self, camera, profile):
        if not self.processor.camera_enabled(camera):
            return False
        mode = profile.get("recognition_mode", "event")
        with self.lock:
            if not self.online:
                return False
            return mode == "continuous" or (mode == "presence" and (
                self.counts.get(camera, 0) > 0 or camera in self.active_events.values()))

    def current(self, camera, expected):
        return self.configuration_current(camera, expected) and self.eligible(camera, expected)

    def configuration_current(self, camera, expected):
        profiles = self.processor.camera_profiles
        return bool(profiles and profiles.get(camera) == expected and self.processor.camera_enabled(camera))

    def tick(self, now=None):
        now = time.time() if now is None else now
        profiles = self.profiles()
        with self.lock:
            if (getattr(self.processor.frigate, "supports_live_frames", False) is True
                    and not self.syncing_streams and now >= self.next_stream_sync):
                self.syncing_streams = True
                self.next_stream_sync = now + 5
                self.executor.submit(self._sync_live_cameras)
            if any(p.get("recognition_mode") == "presence" for p in profiles.values()):
                if not self.reconciling and now >= self.next_reconcile and self.online:
                    self.reconciling = True
                    self.next_reconcile = now + 15
                    self.executor.submit(self._reconcile_active)
            # Least recently sampled camera goes first. No unbounded executor queue.
            for camera in sorted(set(profiles) | set(self.cameras), key=lambda c: self.last_started.get(c, 0)):
                if len(self.pending) >= 2:
                    break
                if camera in self.pending:
                    continue
                profile = profiles.get(camera, {})
                enabled = self.eligible(camera, profile)
                if not enabled and camera not in self.cameras:
                    continue
                interval = profile.get("recognition_interval_seconds", .5)
                if enabled and now - self.last_started.get(camera, 0) < interval:
                    continue
                self.pending.add(camera)
                self.last_started[camera] = now
                self.executor.submit(self._run_camera, camera, profile)

    def _sync_live_cameras(self):
        try:
            # Keep presence streams warm while empty; inference still requires
            # a person. Re-read profiles when this bounded job actually runs.
            cameras = [camera for camera, profile in self.profiles().items()
                       if self.online and self.processor.camera_enabled(camera)
                       and profile.get("recognition_mode", "event") != "event"
                       and profile.get("high_resolution")]
            self.processor.frigate.sync_live_cameras(cameras)
        finally:
            with self.lock:
                self.syncing_streams = False

    def _reconcile_active(self):
        response = None
        with self.lock:
            version = self.activity_version
            camera_versions = dict(self.camera_versions)
        try:
            response = self.processor.frigate.request("GET", "/api/events",
                params={"label": "person", "in_progress": 1, "limit": 1000, "include_thumbnails": 0}, timeout=2.0)
            if response.status_code != 200:
                return
            batch = response.json()
            if not isinstance(batch, list):
                return
            active = {row["id"]: row["camera"] for row in batch if isinstance(row, dict)
                      and row.get("id") and row.get("camera") and row.get("label") == "person"
                      and not row.get("end_time")}
            with self.lock:
                # Preserve newer MQTT data per camera; a busy neighbor must not
                # prevent recovery of this camera's missed start/end message.
                if self.activity_version == version:
                    unchanged = {camera for camera in self.profiles()
                                 if self.camera_versions.get(camera, 0) == camera_versions.get(camera, 0)}
                    self.active_events = {eid: cam for eid, cam in self.active_events.items() if cam not in unchanged}
                    self.active_events.update({eid: cam for eid, cam in active.items() if cam in unchanged})
                    for camera in unchanged:
                        self.counts.pop(camera, None)
        except Exception:
            log.debug("Could not reconcile active Frigate people", exc_info=True)
        finally:
            if response is not None:
                response.close()
            with self.lock:
                self.reconciling = False

    def _run_camera(self, camera, profile):
        try:
            self.sample_camera(camera, profile)
        except Exception:
            with self.lock:
                self.status[camera] = {**self.status.get(camera, {}), "state": "error"}
            log.exception("Continuous sampling failed for camera %s", camera)
        finally:
            with self.lock:
                self.pending.discard(camera)

    def _retire(self, run, ids, now):
        for eid in ids:
            state = run["states"].pop(eid, None)
            if state is None:
                continue
            state.update(ended=True, end_time=now, clip_analyzed=True)
            # Reuse existing unknown review, final audit and enriched-event path.
            # These are local sightings, never Frigate object identifiers.
            self.processor.events[eid] = state

    def sample_camera(self, camera, expected):
        p = self.processor
        now = time.time()
        run = self.cameras.get(camera)
        if run and run["profile"] != expected:
            self._retire(run, list(run["states"]), now)
            self.cameras.pop(camera, None)
            run = None
        if not self.current(camera, expected):
            if run:
                self._retire(run, list(run["states"]), now)
                self.cameras.pop(camera, None)
            with self.lock:
                self.status[camera] = {"state": "waiting"}
            return
        if run is None:
            run = {"profile": expected, "tracker": FaceTracker(expected.get("recognition_rearm_seconds", 2)),
                   "states": {}, "fingerprint": None, "visible": []}
            self.cameras[camera] = run
        started = time.perf_counter()
        source = "main_stream" if expected.get("high_resolution") else "detect_stream"
        with self.lock:
            self.status[camera] = {**self.status.get(camera, {}), "state": "fetching", "source": source,
                                   "last_attempt": now}
        content = camera_frame_bytes(p.frigate, camera, expected, timeout=2.0)
        p.record_timing("camera_fetch", time.perf_counter() - started)
        if not self.current(camera, expected):
            return
        if not content:
            with self.lock:
                self.status[camera] = {"state": "no_frame", "source": source, "last_attempt": now}
            return
        img = cv2.imdecode(np.frombuffer(content, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            with self.lock:
                self.status[camera] = {"state": "invalid_frame", "source": source, "last_attempt": now}
            return
        fingerprint = hashlib.sha256(img.tobytes()).hexdigest()
        now = time.time()
        if fingerprint == run["fingerprint"]:
            run["tracker"].touch(run["visible"], now)
            # Same image retains continuity but cannot confirm identity again.
            self._retry_notifications(run)
            with self.lock:
                self.status[camera] = {**self.status.get(camera, {}), "state": "unchanged_frame"}
            return
        priority = p.engine.live_work() if hasattr(p.engine, "live_work") else nullcontext()
        with priority:
            faces = p.engine.faces(img)
            if not self.current(camera, expected):
                return
            if expected.get("mode") == "intercom":
                height, width = img.shape[:2]
                left, top, right, bottom = expected.get("roi", [0, 0, 1, 1])
                faces = [face for face in faces if (
                    left <= float(face.bbox[0] + face.bbox[2]) / 2 / width <= right
                    and top <= float(face.bbox[1] + face.bbox[3]) / 2 / height <= bottom)]
            assignments, expired = run["tracker"].update(faces, now)
            self._retire(run, expired, now)
            run["fingerprint"] = fingerprint
            run["visible"] = [eid for eid, _ in assignments]
            hour = time.localtime().tm_hour
            minimum = expected.get("night_min_face_px" if hour >= 19 or hour < 6 else "min_face_px", p.min_face_px)
            usable_faces = 0
            largest_face_px = 0
            rejected_reasons = {}
            for eid, face in assignments:
                if not self.current(camera, expected):
                    return
                quality = measure_face_quality(img, face, min_face_px=minimum, min_quality=p.min_face_quality)
                largest_face_px = max(largest_face_px, quality.to_dict().get("face_px", 0))
                if not quality.usable:
                    reason = ("small_face" if quality.face_px < minimum else
                              "weak_detection" if quality.detection < .55 else "low_quality")
                    rejected_reasons[reason] = rejected_reasons.get(reason, 0) + 1
                    continue
                usable_faces += 1
                state = run["states"].get(eid)
                if state is None:
                    state = p._new_event_state(eid, camera, start_time=now,
                        continuous_sighting=True, clip_analyzed=True, consensus_started=now)
                    state["liveness_history"] = deque(maxlen=32)
                    run["states"][eid] = state
                if state["done"]:
                    self._improve_evidence(eid, state, img, face, quality)
                    continue
                # Keep recent evidence, not an ever-growing lifetime vote count.
                window = max(10., p.min_confirmations * expected.get("recognition_interval_seconds", .5) * 3)
                if (state["decision"].policy != p.policy
                        or len(state["decision"].observations) >= max(32, p.min_confirmations)
                        or now - state["consensus_started"] > window):
                    state["decision"] = DecisionAccumulator(p.policy)
                    state["liveness_history"].clear()
                    state.pop("best_recognized", None)
                    state["consensus_started"] = now
                state["attempts"] += 1
                state["decision"].policy = p.policy
                state["context_frame"] = img
                state["access1_evidence_ts"] = None
                state["recognition_guard"] = lambda: self.current(camera, expected)
                state["configuration_guard"] = lambda: self.configuration_current(camera, expected)
                p._process_face(eid, state, img, face, quality=quality, source="continuous_camera")
                if state["done"]:
                    p.record_timing("camera_frame_to_recognition", time.perf_counter() - started)
                if p.audit and state["attempts"] % 32 == 0:
                    p.audit.trim_observations(eid, keep=64)
        self._retry_notifications(run)
        with self.lock:
            self.status[camera] = {"state": "faces_rejected" if assignments and not usable_faces else "sampling",
                "source": source, "last_attempt": now, "last_frame_received": now,
                "frame_width": img.shape[1], "frame_height": img.shape[0], "min_face_px": minimum,
                "usable_faces": usable_faces, "largest_face_px": largest_face_px, "rejected_reasons": rejected_reasons,
                "visible_faces": len(assignments), "tracked_faces": len(run["states"]),
                "frame_processing_ms": round((time.perf_counter() - started) * 1000, 2)}

    def _retry_notifications(self, run):
        for eid, state in run["states"].items():
            pending = state.get("notification_payload")
            if (pending and not state.get("notification_complete")
                    and time.perf_counter() >= state.get("notification_next_retry", 0)):
                self.processor._publish_notification(eid, state, pending["decision"], pending["person"],
                    pending["score"], margin=pending["margin"], confirmations=pending["confirmations"])

    def _improve_evidence(self, eid, state, img, face, quality):
        decision = state.get("final_decision")
        if decision is None or decision.status != "recognized":
            return
        p = self.processor
        candidates = p.gallery.match_candidates(face.normed_embedding, limit=2)
        if not candidates:
            return
        slug, _, score = candidates[0]
        runner = candidates[1][2] if len(candidates) > 1 else 0
        if slug != decision.slug or score < p.match_thr or score - runner < p.match_margin:
            return
        guard = state.get("recognition_guard")
        if guard is not None and not guard():
            return
        p.present.setdefault(state["camera"], {})[decision.person] = time.time()
        data = quality.to_dict()
        rank = (float(data.get("score", 0)), float(data.get("sharpness", 0)), float(face.det_score))
        previous = state.get("best_recognized", {}).get(slug)
        if previous is not None and rank <= previous["rank"]:
            return
        crop = crop_face(img, face.bbox).copy()
        state.setdefault("best_recognized", {})[slug] = {"rank": rank, "crop": crop}
        if p.audit:
            p.audit.save_evidence(eid, crop)

    def report(self):
        streams = (self.processor.frigate.live_frames.report()
                   if getattr(self.processor.frigate, "supports_live_frames", False) is True else {})
        with self.lock:
            return {"in_flight": len(self.pending), "max_in_flight": 2,
                    "streams": streams,
                    "cameras": {camera: dict(value) for camera, value in self.status.items()}}
