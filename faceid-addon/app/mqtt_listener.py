"""Frigate-Events per MQTT verarbeiten und Ergebnisse für HA publizieren.

Pipeline: frigate/events (person) -> Snapshot-Crop -> ArcFace -> Galerie-Match
  - Match + runner-up margin + repeated agreement -> Person publizieren
  - Match < unknown_threshold                     -> unbekannt
  - dazwischen / zu wenig Bestätigungen           -> unsicher, Review-Queue
"""
import json
import hashlib
import logging
import math
import queue
import threading
import time
from contextlib import nullcontext
from collections import deque

import paho.mqtt.client as mqtt
import requests
import cv2
import numpy as np

from .engine import FaceEngine, crop_face
from .hires import upgrade_face
from .decision import DecisionAccumulator, DecisionPolicy
from .quality import measure_face_quality
from .clip_analyzer import ClipAnalyzer
from .presence import RecognitionSessionTracker
from .continuous_recognition import ContinuousRecognition
from .media_errors import ClipNotReady

log = logging.getLogger("faceid.mqtt")


class EventProcessor:
    def __init__(
        self, cfg: dict, engine, gallery, frigate, audit=None, *,
        scenario_manager=None, reid=None, dispatcher=None, ai_context=None,
        media_store=None, frame_distributor=None, body_recognition=None,
    ):
        self.cfg = cfg
        self.engine = engine
        self.gallery = gallery
        self.frigate = frigate
        self.media_store = media_store
        self.frame_distributor = frame_distributor
        self.body_recognition = body_recognition
        self.camera_profiles = None
        self.audit = audit
        self.scenario_manager = scenario_manager
        self.reid = reid
        self.dispatcher = dispatcher
        self.ai_context = ai_context
        self.queue: "queue.Queue[dict]" = queue.Queue(maxsize=200)
        self.clip_queue: "queue.Queue[dict]" = queue.Queue(maxsize=50)
        self._jobs_lock = threading.RLock()
        self._deferred_jobs = {}
        self._timing_lock = threading.Lock()
        self._timings = {}
        self.events: dict[str, dict] = {}  # event_id -> Zustand
        self.recent = deque(maxlen=100)  # Ringpuffer für die UI
        self.client: mqtt.Client | None = None
        f = cfg["faceid"]
        self.match_thr = float(f.get("match_threshold", 0.5))
        self.unknown_thr = float(f.get("unknown_threshold", 0.35))
        self.match_margin = float(f.get("match_margin", 0.08))
        self.ignore_margin = float(f.get("ignore_margin", 0.12))
        self.min_confirmations = max(1, int(f.get("min_confirmations", 2)))
        self.min_face_px = int(f.get("min_face_px", 48))
        self.max_attempts = int(f.get("max_attempts", 6))
        if self.unknown_thr >= self.match_thr:
            raise ValueError("unknown_threshold must be lower than match_threshold")
        if self.min_confirmations > self.max_attempts:
            raise ValueError("min_confirmations cannot exceed max_attempts")
        self.retry_secs = float(f.get("retry_seconds", 2.5))
        self.cameras = set(f.get("cameras") or [])
        self.set_sub_label = bool(f.get("set_sub_label", False))
        self.presence_window = float(f.get("presence_window", 120))
        self.recognition_sessions = RecognitionSessionTracker(
            f.get("recognition_session_seconds", 300)
        )
        self.ignore_thr = float(f.get("ignore_threshold", f.get("match_threshold", 0.5)))
        self.ignore_learning = bool(f.get("ignore_learning", True))
        self.hires_enroll = bool(f.get("hires_enroll", True))
        self.clip_analysis = bool(f.get("clip_analysis", True))
        self.clip_retry_attempts = max(1, int(f.get("clip_retry_attempts", 3)))
        self.clip_retry_seconds = max(1.0, float(f.get("clip_retry_seconds", 10)))
        self.min_face_quality = float(f.get("min_face_quality", 0.35))
        # Ereignisse, die Frigate nicht per MQTT meldet (z. B. per API angelegte
        # Kamera-Meldungen als Zuverlaessigkeits-Bruecke), per Abfrage nachziehen.
        self.poll_interval = float(f.get("poll_interval", 0))
        # Frigate darf sein MQTT-Topic umbenennen (topic_prefix in dessen config.yml).
        self.frigate_topic = str(f.get("frigate_topic_prefix", "frigate")).strip("/") or "frigate"
        self._polled: deque = deque(maxlen=500)   # schon gesehene IDs
        self._announced: set = set()              # Kameras mit angemeldetem Sensor
        self._queued_jobs: set[tuple[str, str]] = set()
        # Der Finalizer raeumt self.events nach der Verarbeitung ab — ohne dieses
        # Gedaechtnis haelt der Poller ein fertig verarbeitetes Ereignis fuer neu.
        self._handled: deque = deque(maxlen=1000)
        self.prefix = str(f.get("mqtt_prefix", "faceid")).strip("/") or "faceid"
        self.present: dict[str, dict[str, float]] = {}  # camera -> {person: zuletzt gesehen}
        self._last_presence: dict[str, list] = {}  # zuletzt publizierter Stand je Kamera
        self._person_presence_state: dict[str, bool] = {}
        self._update_policy()
        self.continuous = ContinuousRecognition(self)
        self.clip_analyzer = ClipAnalyzer(
            engine, frigate,
            max_frames=int(f.get("clip_max_frames", 24)),
            max_samples=int(f.get("clip_max_samples", 8)),
            media_store=media_store,
            min_face_px=self.min_face_px,
            min_quality=self.min_face_quality,
            frame_distributor=frame_distributor,
        )

    def _update_policy(self):
        self.policy = DecisionPolicy(
            match_threshold=self.match_thr,
            unknown_threshold=self.unknown_thr,
            match_margin=self.match_margin,
            ignore_threshold=self.ignore_thr,
            ignore_margin=self.ignore_margin,
            min_confirmations=self.min_confirmations,
        )

    def update_decision_policy(self, **updates):
        match_thr = float(updates.get("match_thr", self.match_thr))
        unknown_thr = float(updates.get("unknown_thr", self.unknown_thr))
        min_confirmations = int(updates.get("min_confirmations", self.min_confirmations))
        if unknown_thr >= match_thr:
            raise ValueError("unknown_threshold must be lower than match_threshold")
        if min_confirmations > self.max_attempts:
            raise ValueError("min_confirmations cannot exceed max_attempts")
        for key, value in updates.items():
            if hasattr(self, key):
                setattr(self, key, value)
        self._update_policy()
        for st in self.events.values():
            st["decision"].policy = self.policy

    def _new_event_state(self, eid: str, cam: str, start_time=None, end_time=None, **extra):
        st = {
            "camera": cam, "attempts": 0, "best_score": 0.0, "best_person": None,
            "best_unknown": None, "last_try": 0.0, "done": False,
            "ended": end_time is not None, "created": time.time(),
            "start_time": start_time or time.time(), "end_time": end_time,
            "decision": DecisionAccumulator(self.policy),
            "liveness_history": [], "liveness": None,
            "processing_lock": threading.Lock(),
        }
        st.update(extra)
        if self.audit:
            self.audit.start_event(eid, cam, st["start_time"])
        return st

    # ---------- MQTT ----------

    def start(self):
        m = self.cfg["mqtt"]
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self.prefix)
        if m.get("user"):
            c.username_pw_set(m["user"], m.get("password", ""))
        c.will_set(f"{self.prefix}/status", "offline", retain=True)
        self.client = c
        c.on_connect = self._guard_callback(self._on_connect)
        c.on_message = self._guard_callback(self._on_message)
        c.connect_async(m["host"], int(m.get("port", 1883)), keepalive=60)
        c.loop_start()
        self._check_frigate()
        threading.Thread(target=self._worker, daemon=True, name="faceid-worker").start()
        threading.Thread(target=self._worker, args=("clip",), daemon=True, name="faceid-clip-worker").start()
        threading.Thread(target=self._finalizer, daemon=True, name="faceid-finalizer").start()
        threading.Thread(target=self._live_sampler, daemon=True, name="faceid-live-sampler").start()
        if self.audit:
            self.audit.recover_running_jobs()
            self._dispatch_pending_jobs()
            threading.Thread(
                target=self._job_dispatcher, daemon=True, name="faceid-job-dispatcher"
            ).start()
        if self.poll_interval > 0:
            threading.Thread(target=self._poller, daemon=True, name="faceid-poller").start()

    @staticmethod
    def _guard_callback(callback):
        def guarded(*args, **kwargs):
            try:
                return callback(*args, **kwargs)
            except Exception:
                log.exception("MQTT callback failed; connection remains active")
        return guarded

    def camera_enabled(self, camera: str) -> bool:
        """Return the live UI override, falling back to the legacy allow-list."""
        if self.camera_profiles is not None:
            stored = self.camera_profiles.all().get(camera)
            if isinstance(stored, dict) and "enabled" in stored:
                return bool(stored["enabled"])
        return not self.cameras or camera in self.cameras

    def set_camera_enabled(self, camera: str, enabled: bool) -> dict:
        if self.camera_profiles is None:
            raise RuntimeError("Camera profiles are unavailable")
        profile = self.camera_profiles.set_enabled(camera, enabled)
        if not enabled:
            self.present[camera] = {}
            self._last_presence.pop(camera, None)
            self._publish_presence(camera)
        log.info("camera %s automatic analysis %s", camera, "enabled" if enabled else "disabled")
        return profile

    def _job_dispatcher(self):
        while True:
            time.sleep(2)
            self._dispatch_pending_jobs()

    def _dispatch_pending_jobs(self):
        with self._jobs_lock:
            self._dispatch_pending_jobs_locked()

    def _dispatch_pending_jobs_locked(self):
        if not self.audit:
            return
        jobs = [*self.audit.pending_jobs(kind="snapshot"), *self.audit.pending_jobs(limit=50, kind="clip")]
        for job in jobs:
            eid, kind = job["event_id"], job["kind"]
            if not self.camera_enabled(job["camera"]):
                self.audit.finalize(
                    eid, "camera_disabled", end_ts=job.get("end_ts") or time.time()
                )
                self.audit.complete_job(eid, kind)
                continue
            if eid not in self.events:
                self.events[eid] = self._new_event_state(
                    eid, job["camera"], start_time=job.get("start_ts"),
                    end_time=job.get("end_ts"), recovered=True,
                )
            key = (eid, kind)
            if key in self._queued_jobs:
                continue
            try:
                work_queue = self.clip_queue if kind == "clip" else self.queue
                work_queue.put_nowait({"eid": eid, "kind": kind, "queued_at": time.perf_counter()})
                self._queued_jobs.add(key)
            except queue.Full:
                continue

    def _enqueue_job(self, eid: str, kind: str = "snapshot") -> bool:
        with self._jobs_lock:
            return self._enqueue_job_locked(eid, kind)

    def _enqueue_job_locked(self, eid: str, kind: str = "snapshot") -> bool:
        if (eid, kind) in self._queued_jobs:
            return True
        if self.audit:
            self.audit.queue_job(eid, kind)
            self._dispatch_pending_jobs()
            return (eid, kind) in self._queued_jobs
        try:
            work_queue = self.clip_queue if kind == "clip" else self.queue
            work_queue.put_nowait({"eid": eid, "kind": kind, "queued_at": time.perf_counter()})
            self._queued_jobs.add((eid, kind))
            return True
        except queue.Full:
            return False

    def _flush_deferred_jobs(self):
        with self._jobs_lock:
            for key, item in list(self._deferred_jobs.items()):
                state = self.events.get(item["eid"])
                if state and state["processing_lock"].locked():
                    continue
                try:
                    work_queue = self.clip_queue if item.get("kind") == "clip" else self.queue
                    work_queue.put_nowait(item)
                except queue.Full:
                    continue
                self._deferred_jobs.pop(key, None)

    def _check_frigate(self):
        """Beim Start einmal nachsehen, ob Frigate ueberhaupt antwortet.

        Ohne diese Zeile im Log ist "es erkennt nichts" kaum von "es kommt nichts an"
        zu unterscheiden."""
        url = self.cfg["frigate"]["url"].rstrip("/")
        try:
            r = self.frigate.request("GET", "/api/config", timeout=8)
            if r.status_code != 200:
                log.error("Frigate at %s replied HTTP %s — without snapshots nothing can be recognised", url, r.status_code)
                return
            cams = list((r.json().get("cameras") or {}).keys())
            log.info("Frigate reachable (%s), cameras: %s", url, ", ".join(cams) or "keine")
            if self.cameras:
                unknown = self.cameras - set(cams)
                if unknown:
                    log.warning("Configured camera(s) %s do not exist in Frigate — nothing from these will ever be processed", ", ".join(sorted(unknown)))
        except (requests.RequestException, ValueError) as e:
            log.error("Frigate at %s unreachable: %s — snapshots, and therefore recognition, will fail", url, e)

    def mqtt_health(self):
        """Broker connection state; this does not claim HA has registered entities."""
        return {"connected": bool(self.client and self.client.is_connected())}

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if getattr(reason_code, "is_failure", reason_code != 0):
            log.warning("MQTT connection rejected (%s)", reason_code)
            return
        log.info("MQTT connected (%s), subscribing to %s/events", reason_code, self.frigate_topic)
        client.subscribe(f"{self.frigate_topic}/events")
        client.subscribe(f"{self.frigate_topic}/+/person")
        client.subscribe(f"{self.frigate_topic}/available")
        self.continuous.reconnect()
        client.subscribe(f"{self.prefix}/v1/review/set")
        client.publish(f"{self.prefix}/status", "online", retain=True)
        self._publish_discovery()
        alerts = getattr(self, "alerts", None)
        if alerts is not None:
            alerts.publish_discovery(client)

    def _on_message(self, client, userdata, msg):
        received_monotonic = time.perf_counter()
        if self.continuous.mqtt(msg.topic, msg.payload):
            return
        try:
            payload = json.loads(msg.payload)
        except json.JSONDecodeError:
            return
        if msg.topic == f"{self.prefix}/v1/review/set":
            if not self.audit:
                return
            event_id = str(payload.get("event_id") or "")
            label = str(payload.get("label") or "")
            allowed = {
                person["name"] for person in self.gallery.persons().values()
            } | {"__unknown__"}
            ok = bool(
                event_id and label in allowed
                and self.audit.set_ground_truth(
                    event_id, label, str(payload.get("reviewer") or "HA action")
                )
            )
            client.publish(
                f"{self.prefix}/v1/review/result",
                json.dumps({"event_id": event_id, "label": label, "ok": ok}),
            )
            return
        after = payload.get("after") or {}
        etype = payload.get("type")
        self.continuous.observe_event(after, etype)
        if after.get("label") != "person":
            return
        cam = after.get("camera", "")
        if not self.camera_enabled(cam):
            return
        eid = after.get("id")
        if not eid:
            return
        profile = self.camera_profiles.get(cam) if self.camera_profiles else {}
        continuous_mode = profile.get("recognition_mode", "event") != "event"
        if continuous_mode and not self._live_access_camera({"camera": cam}):
            return
        if eid not in self.events and self.audit and self.audit.was_finalized(eid):
            return
        if eid not in self._handled:
            self._handled.append(eid)
        self._ensure_discovery(cam)
        st = self.events.get(eid)
        if st is None:
            st = self._new_event_state(
                eid, cam, start_time=after.get("start_time") or time.time(),
                mqtt_received_monotonic=received_monotonic,
            )
            self.events[eid] = st
        st["access_only"] = continuous_mode
        if cam != st["camera"]:
            return
        if etype in ("new", "update"):
            st["live_updated_at"] = time.time()
            snapshot = after.get("snapshot")
            timestamp = snapshot.get("frame_time") if isinstance(snapshot, dict) else None
            # Do not substitute after.frame_time, arrival time or event start:
            # the best event image may be older than all three.
            st["live_snapshot_ts"] = (
                timestamp if type(timestamp) in (int, float) and math.isfinite(timestamp)
                and timestamp >= st["start_time"] and 0 <= time.time() - timestamp < 10
                and not getattr(msg, "retain", False) and not after.get("end_time") else None
            )
        if etype == "end":
            st["ended"] = True
            st["end_time"] = after.get("end_time") or time.time()
            if self.audit:
                self.audit.mark_ended(eid, st["end_time"])
        if st["done"] or st["attempts"] >= self.max_attempts:
            return
        if after.get("has_snapshot") and time.time() - st["last_try"] >= self.recognition_interval(cam):
            st["last_try"] = time.time()
            if not self._enqueue_job(eid, "snapshot"):
                log.warning("queue full, skipping event %s", eid)

    # ---------- Verarbeitung ----------

    def recognition_interval(self, camera):
        profile = self.camera_profiles.get(camera) if self.camera_profiles else {}
        return min(self.retry_secs, .25) if profile.get("fast_recognition") else self.retry_secs

    def _live_sampler(self):
        while True:
            time.sleep(.25)
            try:
                self._schedule_live_jobs()
                self.continuous.tick()
            except Exception:
                log.exception("Live frame scheduling failed; access checks remain required")

    def _live_access_camera(self, st):
        profile = self.camera_profiles.get(st["camera"]) if self.camera_profiles else {}
        connector = getattr(self, "access1", None)
        return bool(profile.get("fast_recognition") and profile.get("high_resolution")
                    and connector is not None and connector.transport is not None)

    def _schedule_live_jobs(self, now=None):
        now = time.time() if now is None else now
        for eid, st in list(self.events.items()):
            pending = st.get("notification_payload")
            if (pending and not st.get("notification_complete")
                    and time.perf_counter() >= st.get("notification_next_retry", 0)):
                self._publish_notification(eid, st, pending["decision"], pending["person"], pending["score"],
                    margin=pending["margin"], confirmations=pending["confirmations"])
            # Only a recent MQTT event may request a bounded live burst. Recovered,
            # ended, disabled and completed events retain the existing archival path.
            updated = st.get("live_updated_at")
            if (updated is None or not 0 <= now - updated < 10
                    or not 0 <= now - st["created"] < 30
                    or st["done"] or st["ended"] or st.get("end_time")
                    or st.get("recovered") or st.get("polled")
                    or st["attempts"] >= self.max_attempts
                    or st.get("live_fetches", 0) >= 12
                    or not self.camera_enabled(st["camera"])
                    or not (self._live_access_camera(st) or (
                        self.camera_profiles and self.camera_profiles.get(st["camera"]).get("fast_recognition")))
                    or now - st["last_try"] < self.recognition_interval(st["camera"])):
                continue
            with self._jobs_lock:
                if (eid, "snapshot") in self._queued_jobs or (eid, "clip") in self._queued_jobs:
                    continue
                if self._enqueue_job_locked(eid, "snapshot"):
                    st["last_try"] = now

    def record_timing(self, stage, seconds):
        with self._timing_lock:
            self._timings.setdefault(stage, deque(maxlen=128)).append(max(0.0, seconds) * 1000)

    def performance(self):
        with self._timing_lock:
            samples = {name: sorted(values) for name, values in self._timings.items() if values}
        report = {name: {"samples": len(values), "p50_ms": round(values[len(values)//2], 2),
                         "p95_ms": round(values[min(len(values)-1, int(len(values)*.95))], 2)}
                  for name, values in samples.items()}
        return {**report, "live_queue": self.queue.qsize(), "clip_queue": self.clip_queue.qsize(),
                "camera_sampling": self.continuous.report()}

    def _worker(self, kind="snapshot"):
        work_queue = self.clip_queue if kind == "clip" else self.queue
        while True:
            self._work_once(work_queue)

    def _work_once(self, work_queue):
        item = work_queue.get()
        eid = item["eid"]
        kind = item.get("kind", "snapshot")
        started = time.perf_counter()
        state = self.events.get(eid)
        event_lock = state.get("processing_lock") if state else None
        locked = False
        deferred = False
        try:
            # Never discard evidence on lock contention or stall other cameras.
            if event_lock is not None:
                locked = event_lock.acquire(blocking=kind == "clip")
                if not locked:
                    with self._jobs_lock:
                        self._deferred_jobs[(eid, kind)] = item
                    deferred = True
                    return
            self.record_timing(f"{kind}_queue_wait", started - item.get("queued_at", started))
            if self.audit:
                self.audit.mark_job_running(eid, kind)
            state = self.events.get(eid)
            if state is not None and not self.camera_enabled(state["camera"]):
                if self.audit:
                    self.audit.finalize(
                        eid, "camera_disabled",
                        end_ts=state.get("end_time") or time.time(),
                    )
                    self.audit.complete_job(eid, kind)
                state["done"] = True
                return
            if kind == "clip":
                background = self.engine.background_work() if hasattr(self.engine, "background_work") else nullcontext()
                with background:
                    self._process_clip(eid)
            else:
                live = (state is not None and not state.get("ended") and not state.get("end_time")
                        and self._live_access_camera(state))
                priority = self.engine.live_work() if live and hasattr(self.engine, "live_work") else nullcontext()
                with priority:
                    self._process(eid)
            if self.audit:
                self.audit.complete_job(eid, kind)
        except ClipNotReady as e:
            state = self.events.get(eid)
            retries = int((state or {}).get("clip_not_ready_retries", 0)) + 1
            if state is not None:
                state["clip_not_ready_retries"] = retries
                state["clip_queued"] = False
            status = "pending"
            if self.audit:
                self.audit.observation(
                    eid, int((state or {}).get("attempts", 0)),
                    "clip_not_ready", source="clip",
                )
                status = self.audit.retry_job(
                    eid, kind, str(e), delay=self.clip_retry_seconds,
                    max_attempts=self.clip_retry_attempts,
                )
            if status == "failed" or retries >= self.clip_retry_attempts:
                if state is not None:
                    state["clip_analyzed"] = True
                log.warning(
                    "event %s: clip remained unavailable after %d attempts; continuing without clip evidence",
                    eid, retries,
                )
            else:
                log.info(
                    "event %s: clip not ready; retry %d/%d in %.0fs",
                    eid, retries + 1, self.clip_retry_attempts,
                    self.clip_retry_seconds,
                )
        except Exception as e:
            if self.audit:
                self.audit.retry_job(eid, kind, str(e))
            log.exception("error while handling event %s (%s)", eid, kind)
        finally:
            if locked:
                event_lock.release()
            self.record_timing(kind, time.perf_counter() - started)
            with self._jobs_lock:
                if not deferred:
                    self._queued_jobs.discard((eid, kind))
            work_queue.task_done()
            self._flush_deferred_jobs()

    def _process_clip(self, eid: str):
        st = self.events.get(eid)
        if st is None or st["done"]:
            return
        st["clip_queued"] = False
        reference = (
            st["best_unknown"]["emb"] if st.get("best_unknown") is not None else None
        )
        profile = self.camera_profiles.get(st["camera"]) if self.camera_profiles else None
        hour = time.localtime().tm_hour
        min_face_px = (
            profile["night_min_face_px"] if profile and (hour >= 19 or hour < 6)
            else profile["min_face_px"] if profile else self.min_face_px
        )
        samples = self.clip_analyzer.analyze(
            eid, reference_embedding=reference, min_face_px=min_face_px,
            roi=profile.get("roi") if profile and profile.get("mode") == "intercom" else None,
        )
        st["clip_analyzed"] = True
        log.info("event %s: clip analysis produced %d diverse face sample(s)", eid, len(samples))
        for sample in samples:
            if st["done"]:
                break
            st["attempts"] += 1
            self._process_face(
                eid, st, sample.frame, sample.face,
                quality=sample.quality, source=f"clip:{sample.frame_index}",
            )

    def _process(self, eid: str):
        st = self.events.get(eid)
        if st is None or st["done"]:
            return
        try:
            self._process_snapshot(eid)
        except Exception:
            st.pop("candidate_fingerprint", None)
            st.pop("candidate_live", None)
            st.pop("candidate_snapshot_ts", None)
            raise
        else:
            timestamp = st.pop("candidate_snapshot_ts", None)
            if timestamp is not None:
                st["live_snapshot_consumed_ts"] = timestamp
            fingerprint = st.pop("candidate_fingerprint", None)
            if fingerprint is not None:
                st["snapshot_fingerprint"] = fingerprint
                if st.pop("candidate_live", False):
                    st.setdefault("live_fingerprints", deque(maxlen=12)).append(fingerprint)

    def _process_snapshot(self, eid: str):
        st = self.events.get(eid)
        if st is None or st["done"]:
            return
        camera_profile = (
            self.camera_profiles.get(st["camera"])
            if self.camera_profiles is not None else None
        )
        if camera_profile and camera_profile.get("recognition_mode", "event") != "event":
            if not self._live_access_camera(st):
                st.update(done=True, clip_analyzed=True)
                return
            st["access_only"] = True
        img = None
        fetch_started = time.perf_counter()
        source = "snapshot"
        st["access1_evidence_ts"] = None
        live_access = (self._live_access_camera(st) and not st["ended"]
                       and not st.get("end_time") and not st.get("recovered") and not st.get("polled"))
        notification_live = bool(camera_profile and camera_profile.get("fast_recognition")
                                 and not st["ended"] and not st.get("end_time")
                                 and not st.get("recovered") and not st.get("polled"))
        if live_access:
            if (st.get("live_fetches", 0) >= 12 or time.time() - st["created"] >= 30
                    or not 0 <= time.time() - st.get("live_updated_at", 0) < 10):
                return
            timestamp = st.get("live_snapshot_ts")
            if (type(timestamp) not in (int, float) or not math.isfinite(timestamp)
                    or timestamp < st["start_time"] or not 0 <= time.time() - timestamp < 10):
                if not st.get("live_timestamp_warned"):
                    log.info("event %s (%s): live_snapshot_timestamp_missing_or_stale", eid, st["camera"])
                    st["live_timestamp_warned"] = True
                return
            st["live_timestamp_warned"] = False
            if timestamp <= st.get("live_snapshot_consumed_ts", 0):
                return
            st["live_fetches"] = st.get("live_fetches", 0) + 1
            img = self.frigate.live_event_frame(eid)
            if img is None:
                self.record_timing("snapshot_fetch", time.perf_counter() - fetch_started)
                return
            # Preserve the timestamp taken BEFORE the request. A newer MQTT image
            # may arrive during I/O; it must not refresh this image's age.
            if (st["ended"] or st.get("end_time") or st["done"]
                    or not self.camera_enabled(st["camera"])
                    or not 0 <= time.time() - timestamp < 10):
                log.info("event %s (%s): live_frame_expired_or_ended during fetch", eid, st["camera"])
                return
            source = "secure_live_snapshot"
            st["access1_evidence_ts"] = timestamp
            st["candidate_snapshot_ts"] = timestamp
        elif notification_live:
            # Event snapshots retain Frigate's best person image and can stay
            # unchanged while the person turns toward the camera. Notification
            # sampling needs current frames; these never carry admission proof.
            if st.get("live_fetches", 0) >= 12 or time.time() - st["created"] >= 30:
                return
            st["live_fetches"] = st.get("live_fetches", 0) + 1
            content = self.frigate.latest_frame_bytes(st["camera"], timeout=2.0)
            if content:
                img = cv2.imdecode(np.frombuffer(content, np.uint8), cv2.IMREAD_COLOR)
                if img is not None:
                    source = "live_camera"
            if st["ended"] or st.get("end_time") or st["done"] or not self.camera_enabled(st["camera"]):
                return
            if img is None:
                img = self.frigate.snapshot(eid, crop=not camera_profile.get("high_resolution"))
        if not live_access and not notification_live and img is None and camera_profile and camera_profile.get("high_resolution"):
            # Preserve the non-access/enrollment-compatible capture path.
            timestamps = [time.time(), float(st.get("start_time") or time.time())]
            for timestamp in timestamps:
                img = self.frigate.recording_frame(st["camera"], timestamp)
                if img is not None:
                    # A requested recording timestamp is not verified capture
                    # metadata. Keep it usable for recognition, never admission.
                    source = "recording"
                    break
        if img is None:
            img = self.frigate.snapshot(eid, crop=True)
        self.record_timing("snapshot_fetch", time.perf_counter() - fetch_started)
        if img is None:
            st["attempts"] += 1
            if self.audit:
                self.audit.observation(eid, st["attempts"], "no_snapshot")
            log.info("event %s (%s): no snapshot from Frigate", eid, st["camera"])
            return
        fingerprint = hashlib.sha256(img.tobytes()).hexdigest()
        if fingerprint == st.get("snapshot_fingerprint") or (live_access and fingerprint in st.get("live_fingerprints", ())):
            return
        st["candidate_fingerprint"] = fingerprint
        st["candidate_live"] = live_access
        st["attempts"] += 1
        st["context_frame"] = img
        # Live access selects by ROI/quality before computing any embeddings.
        found = (self.engine.detect_faces(img)
                 if live_access and hasattr(self.engine, "detect_faces")
                 else self.engine.faces(img))
        if source == "live_camera" and any(
            other_eid != eid and other.get("camera") == st["camera"]
            and not other.get("ended") and not other.get("end_time")
            for other_eid, other in list(self.events.items())
        ):
            # Even one visible face is ambiguous when another tracked person
            # remains present, including after that person's recognition ended.
            if self.audit:
                self.audit.observation(eid, st["attempts"], "multiple_events", source=source)
            log.info("event %s (%s): attempt %d, multiple active person events for current camera frame",
                     eid, st["camera"], st["attempts"])
            return
        if source == "live_camera" and len(found) > 1:
            # Current camera frames have no event-specific person crop. Choosing
            # the clearest face could attribute a bystander to this event.
            if self.audit:
                self.audit.observation(eid, st["attempts"], "multiple_faces", source=source)
            log.info("event %s (%s): attempt %d, multiple faces in current camera frame",
                     eid, st["camera"], st["attempts"])
            return
        if camera_profile and camera_profile.get("mode") == "intercom":
            height, width = img.shape[:2]
            left, top, right, bottom = camera_profile.get("roi", [0, 0, 1, 1])
            found = [face for face in found if (
                left <= float(face.bbox[0] + face.bbox[2]) / 2 / max(width, 1) <= right
                and top <= float(face.bbox[1] + face.bbox[3]) / 2 / max(height, 1) <= bottom
            )]
        hour = time.localtime().tm_hour
        camera_min_face_px = (
            camera_profile["night_min_face_px"]
            if camera_profile and (hour >= 19 or hour < 6)
            else camera_profile["min_face_px"] if camera_profile else self.min_face_px
        )
        measured = [
            (measure_face_quality(
                img, item, min_face_px=camera_min_face_px,
                min_quality=self.min_face_quality,
            ), item)
            for item in found
        ]
        usable = [(quality, item) for quality, item in measured if quality.usable]
        if not usable:
            # Haeufigster Normalfall (Ruecken zur Kamera, zu weit weg) — trotzdem
            # protokollieren, sonst sieht ein stiller Log wie ein Defekt aus. Die
            # Unterscheidung "zu klein" vs. "gar keins" entscheidet, wo man sucht:
            # zu klein deutet auf Frigates snapshots.height oder Kameraabstand,
            # gar keins eher auf Blickwinkel oder Licht.
            h, w = img.shape[:2]
            if found:
                big = max(int(f.bbox[2] - f.bbox[0]) for f in found)
                best_quality = max((quality.score for quality, _ in measured), default=0.0)
                if self.audit:
                    self.audit.observation(
                        eid, st["attempts"], "low_quality", face_px=big,
                        quality=best_quality,
                    )
                log.info(
                    "event %s (%s): attempt %d, no usable face (largest %dpx, "
                    "best quality %.3f, snapshot %dx%d)",
                    eid, st["camera"], st["attempts"], big, best_quality, w, h,
                )
            else:
                if self.audit:
                    self.audit.observation(eid, st["attempts"], "no_face")
                log.info("event %s (%s): attempt %d, no face detected in snapshot %dx%d",
                         eid, st["camera"], st["attempts"], w, h)
            return
        quality, face = max(usable, key=lambda item: item[0].score)
        self._process_face(eid, st, img, face, quality=quality, source=source)

    def _process_face(self, eid: str, st: dict, img, face, quality=None, source="snapshot"):
        if st["done"]:
            return
        guard = st.get("recognition_guard")
        if guard is not None and not guard():
            return
        pending_live_identity = False
        liveness_service = getattr(self, "liveness", None)
        profile = self.camera_profiles.get(st["camera"]) if self.camera_profiles else {}
        liveness_mode = profile.get("liveness_mode", "advisory")
        if liveness_service is not None and liveness_mode != "off":
            scheduled = self.engine.model_work() if hasattr(self.engine, "model_work") else nullcontext()
            with scheduled:
                liveness_frame = liveness_service.analyze(img, face)
            st["liveness_history"].append(liveness_frame)
            st["liveness"] = liveness_service.consensus(st["liveness_history"])
            st["liveness"]["mode"] = liveness_mode
            if self.audit:
                self.audit.update_liveness(eid, st["liveness"])
            if liveness_mode == "required" and not st["liveness"]["confirmed"]:
                state = st["liveness"]["state"]
                st["liveness_blocked"] = True
                if self.audit:
                    self.audit.observation(
                        eid, st["attempts"], f"liveness_{state}",
                        det_score=float(face.det_score),
                        face_px=int(min(face.bbox[2] - face.bbox[0], face.bbox[3] - face.bbox[1])),
                        quality=float(quality.score) if quality else 0.0,
                        source=source,
                    )
                    self.audit.save_evidence(eid, crop_face(img, face.bbox))
                log.warning(
                    "event %s (%s): identity blocked by required liveness (%s, %s/%s live frames)",
                    eid, st["camera"], state, st["liveness"]["live_frames"],
                    st["liveness"]["required_frames"],
                )
                # Collect identity evidence on individually live, fresh frames
                # while consensus warms up. Never publish or enqueue until BOTH
                # existing liveness and identity policies have passed.
                timestamp = st.get("access1_evidence_ts")
                pending_live_identity = (
                    source == "secure_live_snapshot"
                    and liveness_frame.get("live") is True
                    and state == "pending"
                    and not st.get("ended") and not st.get("end_time")
                    and type(timestamp) in (int, float)
                    and math.isfinite(timestamp)
                    and 0 <= time.time() - timestamp < 10
                )
                if not pending_live_identity:
                    return
            if st["liveness"].get("confirmed"):
                st["liveness_blocked"] = False
        if source == "secure_live_snapshot" and hasattr(self.engine, "embed_face"):
            self.engine.embed_face(img, face)
        emb = face.normed_embedding
        candidates = self.gallery.match_candidates(emb, limit=3)
        guest_access = getattr(self, "guest_access", None)
        if guest_access is not None:
            guest_candidates = guest_access.candidates(
                emb, camera=st["camera"], now=st.get("start_time"), limit=2,
            )
            combined = sorted([*candidates, *guest_candidates], key=lambda item: item[2], reverse=True)
            if combined and str(combined[0][0]).startswith("guest:"):
                lead = float(combined[0][2]) - float(combined[1][2] if len(combined) > 1 else 0)
                if float(combined[0][2]) >= guest_access.threshold and lead >= guest_access.margin:
                    candidates = combined[:2]
                else:
                    candidates = [item for item in combined if not str(item[0]).startswith("guest:")][:2]
        slug, name, score = candidates[0] if candidates else (None, None, 0.0)
        _, runner_up, runner_up_score = (
            candidates[1] if len(candidates) > 1 else (None, None, 0.0)
        )
        ig, _, ignore_group = self.gallery.match_ignored_detail(emb)
        if guard is not None and not guard():
            return
        observation_key = hashlib.sha256(emb.astype("float32").tobytes()).hexdigest()
        decision = st["decision"].add(
            key=observation_key,
            slug=slug,
            person=name,
            score=float(score),
            runner_up=runner_up,
            runner_up_score=float(runner_up_score),
            ignore_score=float(ig),
            ignore_group=ignore_group,
        )
        quality_data = quality.to_dict() if quality else {}
        evidence_rank = (float(quality_data.get("score", 0)),
                         float(quality_data.get("sharpness", 0)), float(face.det_score))
        if decision.status in ("recognized_candidate", "recognized"):
            evidence = st.setdefault("best_recognized", {})
            previous = evidence.get(slug)
            if previous is None or evidence_rank > previous["rank"]:
                evidence[slug] = {"rank": evidence_rank, "crop": crop_face(img, face.bbox).copy()}
        if pending_live_identity:
            return
        if decision.status == "recognized":
            st["confirmed_monotonic"] = time.perf_counter()
            if st.get("mqtt_received_monotonic") is not None:
                self.record_timing("mqtt_to_recognition", st["confirmed_monotonic"] - st["mqtt_received_monotonic"])
        if getattr(self, "face_search", None) is not None:
            # Bound transient samples; keep JPEG/SQL work out of consensus warmup.
            st.setdefault("pending_face_samples", deque(maxlen=8)).append((emb.copy(), crop_face(img, face.bbox).copy()))
        face_px = int(min(face.bbox[2] - face.bbox[0], face.bbox[3] - face.bbox[1]))
        observation = dict(person=name, score=float(score), runner_up=runner_up,
            runner_up_score=float(runner_up_score), margin=float(decision.margin),
            ignore_score=float(ig), det_score=float(face.det_score), face_px=face_px,
            quality=float(quality.score) if quality else 0.0, source=source)
        if self.audit and decision.status != "recognized":
            self.audit.observation(eid, st["attempts"], decision.status, **observation)

        if decision.status == "ignored":
            self._flush_face_samples(eid, st)
            if self.audit:
                self.audit.save_evidence(eid, crop_face(img, face.bbox))
            st["best_unknown"] = None
            st["done"] = True
            st["final_decision"] = decision
            if self.ignore_learning and ig >= self.ignore_thr + 0.1 and ig - score >= self.ignore_margin:
                iid = self.gallery.add_ignore_anchor(crop_face(img, face.bbox), emb)
                if iid:
                    log.info("event %s: new auto ignore anchor %s (sim %.3f)", eid, iid, ig)
            if self.audit:
                self.audit.finalize(
                    eid, "ignored", end_ts=st.get("end_time"), score=ig,
                    margin=decision.margin, confirmations=decision.confirmations,
                )
            log.info(
                "event %s (%s): ignored after %d confirmations (sim %.3f, margin %.3f)",
                eid, st["camera"], decision.confirmations, ig, decision.margin,
            )
            return

        crop = crop_face(img, face.bbox)
        log.info(
            "event %s (%s): attempt %d/%s, %s=%s %.3f, runner-up=%s %.3f, "
            "margin %.3f, quality %.3f",
            eid, st["camera"], st["attempts"], source, decision.status, name, score,
            runner_up, runner_up_score, decision.margin,
            quality.score if quality else 0.0,
        )

        if decision.status == "recognized":
            occurrence = self.recognition_sessions.classify(
                decision.person, st["camera"], st.get("start_time")
            )
            if st.get("continuous_sighting") and occurrence == "presence_update":
                occurrence = "arrival"
            st["recognition_occurrence"] = occurrence
            actionable = occurrence != "presence_update"
            st["best_score"], st["best_person"] = decision.score, decision.person
            st["done"] = True
            st["final_decision"] = decision
            if self.audit:
                # Notifications and realtime alerts can trigger an immediate
                # image fetch. Publish the chosen crop first, including updates
                # within an existing presence session.
                evidence = st.get("best_recognized", {}).get(decision.slug, {})
                self.audit.save_evidence(eid, evidence.get("crop", crop))
            self._publish_notification(eid, st, decision.status, decision.person, decision.score,
                margin=decision.margin, confirmations=decision.confirmations)
            access1 = getattr(self, "access1", None)
            if access1 is not None and not st.get("continuous_sighting"):
                access1.enqueue({
                    "event_id": eid, "slug": decision.slug, "camera": st["camera"],
                    "score": decision.score, "occurred_at": st.get("start_time"),
                    "queued_at": time.time(),
                    "evidence_source": source,
                    "evidence_ts": st.get("access1_evidence_ts"),
                    "event_ended": bool(st.get("end_time")),
                    "liveness_confirmed": bool((st.get("liveness") or {}).get("confirmed")),
                    "liveness_score": (st.get("liveness") or {}).get("score"),
                    "liveness_frames": (st.get("liveness") or {}).get("live_frames", 0),
                })
            self._publish_recognition(eid, st, decision.person, decision.score, decision=decision)
            self._flush_face_samples(eid, st)
            if self.audit:
                self.audit.observation(eid, st["attempts"], decision.status, **observation)
            if actionable and not st.get("continuous_sighting") and decision.slug and str(decision.slug).startswith("guest:"):
                guest_id = str(decision.slug).split(":", 1)[1]
                st["guest"] = {"id": guest_id, "name": decision.person}
                access_result = guest_access.evaluate(
                    guest_id, camera=st["camera"], score=decision.score,
                    runner_up_score=decision.runner_up_score,
                    liveness_confirmed=bool((st.get("liveness") or {}).get("confirmed")),
                    second_factor=False, event_id=eid,
                )
                st["guest_access"] = access_result
                if self.client:
                    self.client.publish(
                        f"{self.prefix}/v1/guest_access",
                        json.dumps(access_result, ensure_ascii=False), retain=False,
                    )
            if actionable and self.reid is not None and not st.get("continuous_sighting"):
                self.reid.seed(
                    decision.person, st["camera"], st.get("context_frame"),
                    ts=st.get("start_time"),
                )
            if self.audit:
                self.audit.finalize(
                    eid, "recognized", end_ts=st.get("end_time"),
                    person=decision.person, score=decision.score,
                    margin=decision.margin, confirmations=decision.confirmations,
                    occurrence=occurrence,
                    **({"recognition_ts": time.time()} if st.get("continuous_sighting") else {}),
                )
            return

        # Keep the best review image until the event ends. A high but insufficiently
        # confirmed match is ambiguous, never silently promoted or ignored.
        prev = st.get("best_unknown")
        if prev is None or evidence_rank > prev["rank"]:
            st["best_unknown"] = {
                "rank": evidence_rank,
                "crop": crop, "emb": emb, "det_score": float(face.det_score),
                "guess": name, "guess_score": float(score), "full": img,
                "decision": st["decision"].pending_status(),
                "runner_up": runner_up, "runner_up_score": float(runner_up_score),
                "margin": float(decision.margin),
                "quality": quality_data,
            }

    def _poller(self):
        """Frigate-Ereignisse abfragen, die per MQTT nie ankommen.

        Manuell ueber die API angelegte Ereignisse sind fuer Frigate keine getrackten
        Objekte und loesen ``frigate/events`` nicht aus — eine Kamera-eigene
        Personenmeldung, die als Bruecke ein Ereignis anlegt, bliebe sonst ungenutzt.
        Sie haben keine Bounding-Box, der Snapshot ist also das Vollbild; die Erkennung
        laeuft ansonsten durch dieselbe Pipeline.
        """
        url = self.cfg["frigate"]["url"].rstrip("/")
        # Beim Start nicht die halbe Historie aufrollen.
        since = time.time() - min(self.poll_interval * 4, 300)
        while True:
            time.sleep(self.poll_interval)
            try:
                r = self.frigate.request(
                    "GET", "/api/events",
                    params={"label": "person", "has_snapshot": 1,
                            "limit": 50, "after": since - 30},
                    timeout=10,
                )
                if r.status_code != 200:
                    continue
                batch = r.json()
            except (requests.RequestException, ValueError) as e:
                log.debug("poll failed: %s", e)
                continue
            since = time.time()
            for ev in batch:
                eid = ev.get("id")
                if (not eid or eid in self._polled or eid in self.events
                        or eid in self._handled
                        or (self.audit and self.audit.was_finalized(eid))):
                    continue
                cam = ev.get("camera", "")
                if not self.camera_enabled(cam):
                    continue
                if self.camera_profiles and self.camera_profiles.get(cam).get("recognition_mode", "event") != "event":
                    continue
                self._polled.append(eid)
                self._ensure_discovery(cam)
                # Nur abgeschlossene Ereignisse — laufende meldet MQTT ohnehin.
                if not ev.get("end_time"):
                    continue
                self.events[eid] = self._new_event_state(
                    eid, cam,
                    start_time=ev.get("start_time") or time.time(),
                    end_time=ev.get("end_time"),
                    polled=True,
                )
                log.info("poll: picked up event %s (%s) — never announced over MQTT",
                         eid, cam)
                if not self._enqueue_job(eid, "snapshot"):
                    log.warning("queue full — dropped polled event %s", eid)

    def _finalizer(self):
        """Beendete Events abschließen: Unknown ablegen, 'unbekannt' melden, aufräumen."""
        while True:
            time.sleep(5)
            now = time.time()
            for cam in list(self.present.keys()):
                self._publish_presence(cam)  # abgelaufene Personen austragen -> ggf. 'niemand'
            self._finalize_events(now)

    def _finalize_events(self, now=None):
        now = time.time() if now is None else now
        for eid in list(self.events.keys()):
            st = self.events.get(eid)
            if st is None:
                continue
            with self._jobs_lock:
                if any(key in self._queued_jobs for key in ((eid, "snapshot"), (eid, "clip"))):
                    continue
            event_lock = st["processing_lock"]
            if not event_lock.acquire(blocking=False):
                continue
            try:
                expired = now - st["created"] > 600
                if not (st["ended"] or expired):
                    continue
                if now - st["last_try"] < self.retry_secs + 1 and not expired:
                    continue  # letzter Versuch evtl. noch in der Queue
                if (
                    self.clip_analysis
                    and not st["done"]
                    and not st.get("clip_analyzed")
                ):
                    if st.get("clip_queued"):
                        continue
                    try:
                        if not self._enqueue_job(eid, "clip"):
                            raise queue.Full
                        st["clip_queued"] = True
                        continue
                    except queue.Full:
                        log.info("clip analysis waiting for retry or queue capacity for %s", eid)
                        continue
                final_status = None
                final_person = None
                final_score = 0.0
                final_margin = 0.0
                confirmations = 0
                if st["best_person"] is None and st["best_unknown"] is not None:
                    u = st["best_unknown"]
                    confirmations = sum(
                        1 for observation in st["decision"].observations
                        if observation.get("person") == u.get("guess")
                    )
                    crop, emb, full = u["crop"], u["emb"], u.get("full")
                    if self.hires_enroll and not st.get("continuous_sighting"):
                        # schärferes Gesicht aus der Aufnahme holen (bessere Referenz)
                        try:
                            hi = upgrade_face(self.engine, self.frigate, st["camera"],
                                              st.get("start_time"), st.get("end_time"), emb,
                                              event_id=eid)
                        except Exception:
                            hi = None
                        if hi is not None:
                            face, img = hi
                            old_w = int(u["crop"].shape[1])
                            new_w = int(face.bbox[2] - face.bbox[0])
                            if new_w > old_w:
                                crop, emb, full = crop_face(img, face.bbox), face.normed_embedding, img
                                log.info("event %s: sharper reference from the recording (%dpx instead of %dpx)",
                                         eid, new_w, old_w)
                    uid = self.gallery.save_unknown(
                        crop, emb,
                        {"camera": st["camera"], "event_id": eid,
                         "event_ts": st.get("start_time"),
                         "guess": u["guess"], "guess_score": round(u["guess_score"], 3),
                         "decision": u.get("decision", "unknown"),
                         "runner_up": u.get("runner_up"),
                         "runner_up_score": round(u.get("runner_up_score", 0.0), 3),
                         "margin": round(u.get("margin", 0.0), 3)},
                        full_bgr=full,
                    )
                    if self.audit:
                        self.audit.save_evidence(eid, crop)
                    final_status = u.get("decision", "unknown")
                    final_person = u.get("guess")
                    final_score = u["guess_score"]
                    final_margin = u.get("margin", 0.0)
                    self._publish_recognition(
                        eid, st, "unknown", u["guess_score"],
                        decision_status=final_status,
                    )
                    if self.audit:
                        self.audit.finalize(
                            eid, final_status, end_ts=st.get("end_time"),
                            person=u.get("guess"), score=u["guess_score"],
                            margin=u.get("margin", 0.0),
                            confirmations=confirmations,
                        )
                    log.info("event %s: %s face stored (%s)", eid, final_status, uid)
                elif self.audit:
                    decision = st.get("final_decision")
                    if decision is not None:
                        final_status = decision.status
                        final_person = decision.person
                        final_score = decision.score
                        final_margin = decision.margin
                        confirmations = decision.confirmations
                        self.audit.finalize(
                            eid, decision.status, end_ts=st.get("end_time"),
                            person=decision.person, score=decision.score,
                            margin=decision.margin,
                            confirmations=decision.confirmations,
                            occurrence=st.get("recognition_occurrence"),
                        )
                    elif not self.audit.was_finalized(eid):
                        liveness_state = (st.get("liveness") or {}).get("state")
                        final_status = (
                            "spoof_suspected" if liveness_state == "spoof"
                            else "liveness_unconfirmed" if st.get("liveness_blocked")
                            else "no_face"
                        )
                        self.audit.finalize(
                            eid, final_status, end_ts=st.get("end_time")
                        )
                if final_status is None:
                    decision = st.get("final_decision")
                    if decision is not None:
                        final_status = decision.status
                        final_person = decision.person
                        final_score = decision.score
                        final_margin = decision.margin
                        confirmations = decision.confirmations
                    else:
                        liveness_state = (st.get("liveness") or {}).get("state")
                        final_status = (
                            "spoof_suspected" if liveness_state == "spoof"
                            else "liveness_unconfirmed" if st.get("liveness_blocked")
                            else "no_face"
                        )
                self._post_event(
                    eid, st, final_status, final_person, final_score,
                    margin=final_margin, confirmations=confirmations,
                )
                self.events.pop(eid, None)
            finally:
                event_lock.release()
                self._flush_deferred_jobs()

    # ---------- Publish ----------

    def _post_event(
        self, eid: str, st: dict, status: str, person: str | None,
        score: float, *, margin: float = 0.0, confirmations: int = 0,
    ):
        """Publish one enriched final event after all frame/clip evidence is known."""
        if st.get("access_only"):
            return
        guard = st.get("configuration_guard")
        if guard is not None and not guard():
            return
        if st.get("post_processed"):
            return
        st["post_processed"] = True
        self._publish_notification(eid, st, status, person, score, margin=margin, confirmations=confirmations)
        self._flush_face_samples(eid, st)
        if self.set_sub_label and status == "recognized" and person and not st.get("continuous_sighting"):
            try:
                self.frigate.set_sub_label(eid, person, score)
            except Exception:
                log.exception("Could not update Frigate label for event %s", eid)
        if self.body_recognition is not None and st.get("context_frame") is not None and not st.get("continuous_sighting"):
            try:
                st["body"] = self.body_recognition.predict(st["context_frame"], eid)
            except Exception as exc:
                st["body"] = {"advisory": True, "error": str(exc)[:160]}
                log.warning("event %s: advisory body path failed: %s", eid, exc)
        occurrence = st.get("recognition_occurrence")
        actionable = occurrence != "presence_update"
        probable_person, probable_score = None, 0.0
        if (
            self.reid is not None
            and not st.get("continuous_sighting")
            and status in {"unknown", "ambiguous", "no_face"}
            and st.get("context_frame") is not None
        ):
            try:
                probable_person, probable_score = self.reid.match(
                    st["camera"], st["context_frame"], ts=st.get("start_time")
                )
            except Exception:
                log.exception("event %s: appearance Re-ID failed", eid)
            if probable_person and self.audit:
                self.audit.update_context(
                    eid, probable_person=probable_person,
                    probable_score=probable_score,
                )
        link_person = person if status == "recognized" else probable_person
        scenario = None
        if actionable and self.scenario_manager is not None:
            try:
                scenario = self.scenario_manager.attach(
                    eid,
                    camera=st["camera"],
                    start_ts=float(st.get("start_time") or time.time()),
                    end_ts=float(st.get("end_time") or time.time()),
                    status=("probable_reid"
                            if probable_person and status != "recognized" else status),
                    person=link_person,
                )
            except Exception:
                log.exception("event %s: scenario attachment failed", eid)
        payload = {
            "event_id": eid,
            "camera": st["camera"],
            "start_ts": st.get("start_time"),
            "end_ts": st.get("end_time") or time.time(),
            "decision": status,
            "person": person if status == "recognized" else None,
            "score": round(float(score or 0), 4),
            "margin": round(float(margin or 0), 4),
            "confirmations": int(confirmations),
            "probable_person": probable_person,
            "probable_score": round(float(probable_score or 0), 4),
            "scenario_id": scenario["scenario_id"] if scenario else None,
            "scenario": scenario,
            "body": st.get("body"),
            "liveness": st.get("liveness"),
            "guest": st.get("guest"),
            "guest_access": st.get("guest_access"),
            "occurrence": occurrence,
        }
        if actionable and self.client and st.get("body"):
            self.client.publish(
                f"{self.prefix}/body/advisory",
                json.dumps({"event_id": eid, "camera": st["camera"], **st["body"]},
                           ensure_ascii=False),
                retain=False,
            )
        if (
            actionable and self.body_recognition is not None
            and not st.get("continuous_sighting")
            and status == "recognized" and person
            and st.get("context_frame") is not None
        ):
            try:
                self.body_recognition.add_pending(
                    eid, st["context_frame"], person, st["camera"], "confirmed-face"
                )
            except Exception:
                log.exception("event %s: could not stage body material", eid)
        if actionable and self.dispatcher is not None:
            try:
                self.dispatcher.dispatch(
                    payload, client=self.client, prefix=self.prefix,
                    webhooks=not bool(st.get("notification_payload")),
                )
            except Exception:
                log.exception("event %s: automation dispatch failed", eid)
        if actionable and self.ai_context is not None:
            try:
                self.ai_context.submit(
                    eid, st.get("context_frame"),
                    camera=st["camera"], decision=status,
                )
            except Exception:
                log.exception("event %s: AI context enqueue failed", eid)
        if status == "recognized" and person:
            self._publish_person_state(person)

    def _flush_face_samples(self, eid, st):
        service = getattr(self, "face_search", None)
        for emb, crop in st.pop("pending_face_samples", ()):
            if service is not None:
                try:
                    service.record(eid, emb, crop)
                except Exception:
                    log.exception("Could not index face for event %s", eid)

    def _publish_notification(self, eid, st, status, person, score, *, margin=0.0, confirmations=0):
        guard = st.get("recognition_guard")
        if guard is not None and not guard():
            st["notification_complete"] = True
            return
        if st.get("access_only") or st.get("notification_complete") or st.get("recognition_occurrence") == "presence_update":
            return
        now = time.perf_counter()
        payload = st.get("notification_payload")
        if payload is not None:
            # Retry only definite local nonacceptance, briefly while still live.
            # Never convert a reconnect or finalization into a stale automation.
            if (now - st["notification_started"] >= 2 or st["ended"]
                    or st.get("recovered") or st.get("polled")):
                st["notification_complete"] = True
                log.warning("Immediate notification expired for event %s", eid)
                return
        else:
            payload = dict(event_id=eid, camera=st["camera"], start_ts=st.get("start_time"),
                end_ts=st.get("end_time"), decision=status,
                person=person if status == "recognized" else None,
                score=round(float(score or 0), 4), margin=round(float(margin or 0), 4),
                confirmations=int(confirmations), liveness=st.get("liveness"),
                occurrence=st.get("recognition_occurrence"), decided_at=time.time(),
                phase="confirmed" if status == "recognized" else "final",
                live=not bool(st.get("ended") or st.get("recovered") or st.get("polled")),
                probable_person=None, probable_score=0.0, scenario_id=None, scenario=None,
                body=st.get("body"), guest=st.get("guest"), guest_access=st.get("guest_access"))
            st["notification_payload"] = payload
            st["notification_started"] = now
        st["notification_next_retry"] = now + .25
        delivery = {}
        client = None if st.get("notification_mqtt_complete") else self.client
        webhooks = not st.get("notification_webhooks")
        try:
            if self.dispatcher is not None:
                self.dispatcher.dispatch(payload, client=client, prefix=self.prefix,
                    topic="v1/recognitions", webhooks=webhooks, delivery=delivery)
            elif client:
                info = client.publish(f"{self.prefix}/v1/recognitions",
                    json.dumps({"schema_version": 1, **payload}, ensure_ascii=False), retain=False)
                rc = getattr(info, "rc", 0)
                delivery.update(mqtt=not isinstance(rc, int) or rc == 0, webhooks=True)
            else:
                delivery.update(mqtt=True, webhooks=True)
            st["notification_mqtt_complete"] = delivery.get("mqtt") is not False
            st["notification_webhooks"] = delivery.get("webhooks", True)
            st["notification_complete"] = st["notification_mqtt_complete"] and st["notification_webhooks"]
            if client and delivery.get("mqtt") is True and st.get("confirmed_monotonic") is not None:
                self.record_timing("confirmed_to_notification", time.perf_counter() - st["confirmed_monotonic"])
        except Exception:
            st["notification_complete"] = True  # uncertain acceptance is not replayable
            log.exception("Could not publish immediate recognition %s", eid)

    def _publish_recognition(
        self, eid: str, st: dict, name: str, score: float,
        decision=None, decision_status: str | None = None,
    ):
        if st.get("access_only"):
            return
        guard = st.get("recognition_guard")
        if guard is not None and not guard():
            return
        payload = {
            "person": name, "score": round(float(score), 3), "camera": st["camera"],
            "event_id": eid, "ts": time.time(),
            "decision": decision_status or (decision.status if decision else name),
            "liveness": st.get("liveness"),
            "occurrence": st.get("recognition_occurrence"),
        }
        if decision is not None:
            payload.update({
                "margin": round(float(decision.margin), 3),
                "confirmations": int(decision.confirmations),
                "runner_up": decision.runner_up,
                "runner_up_score": round(float(decision.runner_up_score), 3),
            })
        self._publish_notification(eid, st, payload["decision"], name, score,
            margin=decision.margin if decision else 0,
            confirmations=decision.confirmations if decision else 0)
        actionable = st.get("recognition_occurrence") != "presence_update"
        if actionable and self.client and st.get("announced") != name:
            st["announced"] = name
            try:
                self.client.publish(f"{self.prefix}/event", json.dumps(payload, ensure_ascii=False), retain=False)
            except Exception:
                log.exception("Could not publish legacy recognition %s", eid)
        alerts = getattr(self, "alerts", None)
        if alerts is not None:
            try:
                alerts.record(event_id=eid, camera=st["camera"], status=payload["decision"],
                              person=name, score=score, ts=st.get("start_time"))
            except Exception:
                log.exception("Could not record recognition alert %s", eid)
        actionable = st.get("recognition_occurrence") != "presence_update"
        if actionable:
            self.recent.appendleft(payload)
        # faceid/event genau einmal pro (Event, Person) — Score-Verbesserungen lösen keine
        # erneute Meldung aus (sonst mehrere Notifications für dieselbe Sichtung)
        self.present.setdefault(st["camera"], {})[name] = time.time()
        self._publish_presence(st["camera"], last=payload)

    def _publish_presence(self, cam: str, last: dict | None = None):
        """Sensor-State = alle im Fenster gesehenen Personen ('Christian, Juli' / 'niemand')."""
        now = time.time()
        pres = self.present.setdefault(cam, {})
        for n, ts in list(pres.items()):
            if now - ts > self.presence_window:
                pres.pop(n)
        names = [n for n, _ in sorted(pres.items(), key=lambda kv: -kv[1])]
        if names == self._last_presence.get(cam) and last is None:
            return  # nichts geändert -> retained Topic nicht neu beschreiben
        self._last_presence[cam] = names
        if self.client:
            attrs = {"persons": names, "window_s": self.presence_window, "ts": now}
            if last:
                attrs["last"] = last
            self.client.publish(f"{self.prefix}/{cam}/person", ", ".join(names) or "nobody", retain=True)
            self.client.publish(f"{self.prefix}/{cam}/attributes", json.dumps(attrs, ensure_ascii=False), retain=True)
        self._refresh_person_presence()

    def _person_slug(self, name: str):
        for slug, person in self.gallery.persons().items():
            if person.get("name") == name:
                return slug
        return None

    def _publish_person_discovery(self, slug: str, name: str):
        if not self.client:
            return
        device = {
            "identifiers": [f"{self.prefix}_person_{slug}"],
            "name": name,
            "manufacturer": "FaceID",
            "model": "Recognized person",
            "via_device": self.prefix,
        }
        common = {
            "availability_topic": f"{self.prefix}/status",
            "device": device,
        }
        entities = [
            ("sensor", "last_location", {
                **common,
                "name": "מיקום אחרון",
                "unique_id": f"{self.prefix}_{slug}_last_location",
                "object_id": f"{self.prefix}_{slug}_last_location",
                "state_topic": f"{self.prefix}/person/{slug}/location",
                "json_attributes_topic": f"{self.prefix}/person/{slug}/attributes",
                "icon": "mdi:map-marker-account",
            }),
            ("sensor", "last_seen", {
                **common,
                "name": "נראה לאחרונה",
                "unique_id": f"{self.prefix}_{slug}_last_seen",
                "object_id": f"{self.prefix}_{slug}_last_seen",
                "state_topic": f"{self.prefix}/person/{slug}/last_seen",
                "device_class": "timestamp",
                "icon": "mdi:clock-outline",
            }),
            ("binary_sensor", "presence", {
                **common,
                "name": "נראה לאחרונה",
                "unique_id": f"{self.prefix}_{slug}_presence",
                "object_id": f"{self.prefix}_{slug}_presence",
                "state_topic": f"{self.prefix}/person/{slug}/presence",
                "payload_on": "ON",
                "payload_off": "OFF",
                "device_class": "presence",
            }),
            ("sensor", "today", {
                **common,
                "name": "הופעות היום",
                "unique_id": f"{self.prefix}_{slug}_today",
                "object_id": f"{self.prefix}_{slug}_today",
                "state_topic": f"{self.prefix}/person/{slug}/today",
                "icon": "mdi:counter",
            }),
            ("sensor", "average_confidence", {
                **common,
                "name": "ביטחון ממוצע",
                "unique_id": f"{self.prefix}_{slug}_average_confidence",
                "object_id": f"{self.prefix}_{slug}_average_confidence",
                "state_topic": f"{self.prefix}/person/{slug}/average_confidence",
                "unit_of_measurement": "%",
                "icon": "mdi:gauge",
            }),
            ("sensor", "appearances", {
                **common,
                "name": "סך הופעות",
                "unique_id": f"{self.prefix}_{slug}_appearances",
                "object_id": f"{self.prefix}_{slug}_appearances",
                "state_topic": f"{self.prefix}/person/{slug}/appearances",
                "icon": "mdi:account-eye",
            }),
            ("sensor", "visits_30_days", {
                **common,
                "name": "ביקורים ב־30 ימים",
                "unique_id": f"{self.prefix}_{slug}_visits_30_days",
                "object_id": f"{self.prefix}_{slug}_visits_30_days",
                "state_topic": f"{self.prefix}/person/{slug}/visits_30_days",
                "icon": "mdi:door-open",
            }),
        ]
        for component, key, config in entities:
            self.client.publish(
                f"homeassistant/{component}/{self.prefix}_{slug}_{key}/config",
                json.dumps(config, ensure_ascii=False),
                retain=True,
            )

    def _publish_person_state(self, name: str):
        if not self.client:
            return
        slug = self._person_slug(name)
        if not slug:
            return
        self._publish_person_discovery(slug, name)
        stats = (
            self.audit.person_statistics().get(name, {}) if self.audit else {}
        )
        visit_stats = (
            self.visits.person_statistics(name)
            if getattr(self, "visits", None) else {}
        )
        active_cameras = sorted(
            camera for camera, people in self.present.items() if name in people
        )
        present = bool(active_cameras)
        last_camera = stats.get("last_camera") or (
            active_cameras[0] if active_cameras else "not seen yet"
        )
        attributes = {
            "person": name,
            "last_seen": stats.get("last_seen"),
            "last_camera": stats.get("last_camera"),
            "last_score": round(float(stats.get("last_score", 0)), 3),
            "average_score": round(float(stats.get("avg_score", 0)), 3),
            "appearances": int(stats.get("appearances", 0)),
            "appearances_today": int(stats.get("today", 0)),
            "appearances_7_days": int(stats.get("last_7_days", 0)),
            "appearances_30_days": int(stats.get("last_30_days", 0)),
            "most_seen_camera": stats.get("top_camera"),
            "camera_breakdown": stats.get("cameras", []),
            "active_cameras": active_cameras,
            "presence_window_seconds": self.presence_window,
            "visits_30_days": int(visit_stats.get("visits", 0)),
            "confirmed_arrivals_30_days": int(
                visit_stats.get("confirmed_arrivals", 0)
            ),
            "confirmed_departures_30_days": int(
                visit_stats.get("confirmed_departures", 0)
            ),
            "average_visit_duration_seconds": visit_stats.get(
                "average_duration_seconds"
            ),
            "common_arrival_hour": visit_stats.get("common_arrival_hour"),
        }
        self.client.publish(
            f"{self.prefix}/person/{slug}/location", last_camera, retain=True
        )
        if stats.get("last_seen"):
            last_seen = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(stats["last_seen"]))
            )
            self.client.publish(
                f"{self.prefix}/person/{slug}/last_seen", last_seen, retain=True
            )
        self.client.publish(
            f"{self.prefix}/person/{slug}/attributes",
            json.dumps(attributes, ensure_ascii=False),
            retain=True,
        )
        self.client.publish(
            f"{self.prefix}/person/{slug}/presence",
            "ON" if present else "OFF",
            retain=True,
        )
        self.client.publish(
            f"{self.prefix}/person/{slug}/today",
            str(attributes["appearances_today"]),
            retain=True,
        )
        self.client.publish(
            f"{self.prefix}/person/{slug}/average_confidence",
            str(round(attributes["average_score"] * 100, 1)),
            retain=True,
        )
        self.client.publish(
            f"{self.prefix}/person/{slug}/appearances",
            str(attributes["appearances"]),
            retain=True,
        )
        self.client.publish(
            f"{self.prefix}/person/{slug}/visits_30_days",
            str(attributes["visits_30_days"]),
            retain=True,
        )
        self._person_presence_state[name] = present

    def _refresh_person_presence(self):
        if not self.client:
            return
        for person in self.gallery.persons().values():
            name = person.get("name")
            present = any(name in people for people in self.present.values())
            if self._person_presence_state.get(name) != present:
                self._publish_person_state(name)

    def _frigate_cameras(self) -> set:
        """Kameranamen von Frigate holen — fuer den Fall, dass keine konfiguriert sind."""
        try:
            r = self.frigate.request("GET", "/api/config", timeout=8)
            if r.status_code == 200:
                return set((r.json().get("cameras") or {}).keys())
        except (requests.RequestException, ValueError) as e:
            log.warning("could not fetch the camera list from Frigate (%s) — sensors will appear once the first person is seen", e)
        return set()

    def _ensure_discovery(self, cam: str):
        """Sensor fuer eine Kamera anlegen, falls noch nicht geschehen."""
        if not cam or cam in self._announced:
            return
        self._announced.add(cam)
        self._publish_discovery([cam])

    def _publish_discovery(self, only: list | None = None):
        """HA MQTT-Discovery: ein Sensor je Kamera (zuletzt erkannte Person).

        Eine leere ``cameras``-Liste bedeutet "alle Kameras verarbeiten" — frueher
        entstanden dann gar keine Sensoren, weil hier ueber eine leere Menge gelaufen
        wurde. Ohne Konfiguration fragen wir deshalb Frigate; klappt auch das nicht,
        legt ``_ensure_discovery`` den Sensor an, sobald die Kamera das erste Mal
        auftaucht."""
        if only is not None:
            cams = set(only)
        else:
            cams = (self.cameras
                    or set(self.cfg["faceid"].get("discovery_cameras") or [])
                    or self._frigate_cameras())
            self._announced |= cams
            log.info("MQTT discovery: announced %d sensor(s)%s", len(cams),
                     "" if cams else " — cameras unknown, they follow on first recognition")
        device = {"identifiers": [self.prefix], "name": self.prefix.replace("-", " ").title() if self.prefix != "faceid" else "FaceID",
                  "manufacturer": "Eigenbau", "model": "InsightFace/ArcFace"}
        event_conf = {
            "name": "Recognition event",
            "unique_id": f"{self.prefix}_recognition_event",
            "object_id": f"{self.prefix}_recognition_event",
            "state_topic": f"{self.prefix}/v1/recognitions",
            "event_types": ["recognized", "unknown", "ambiguous", "no_face", "ignored", "spoof_suspected", "liveness_unconfirmed"],
            "value_template": "{{ value_json.decision }}",
            "json_attributes_topic": f"{self.prefix}/v1/recognitions",
            "availability_topic": f"{self.prefix}/status",
            "icon": "mdi:face-recognition",
            "device": device,
        }
        self.client.publish(
            f"homeassistant/event/{self.prefix}_recognition_event/config",
            json.dumps(event_conf, ensure_ascii=False), retain=True,
        )
        body_conf = {
            "name": "Body evidence (advisory)",
            "unique_id": f"{self.prefix}_body_advisory",
            "object_id": f"{self.prefix}_body_advisory",
            "state_topic": f"{self.prefix}/body/advisory",
            "value_template": "{{ value_json.person or value_json.candidate or 'none' }}",
            "json_attributes_topic": f"{self.prefix}/body/advisory",
            "availability_topic": f"{self.prefix}/status",
            "icon": "mdi:account-search-outline",
            "entity_category": "diagnostic",
            "device": device,
        }
        self.client.publish(
            f"homeassistant/sensor/{self.prefix}_body_advisory/config",
            json.dumps(body_conf, ensure_ascii=False), retain=True,
        )
        for cam in cams:
            conf = {
                "name": cam,  # HA stellt den Gerätenamen "FaceID" voran
                "unique_id": f"{self.prefix}_{cam}",
                "object_id": f"{self.prefix}_{cam}",
                "state_topic": f"{self.prefix}/{cam}/person",
                "json_attributes_topic": f"{self.prefix}/{cam}/attributes",
                "availability_topic": f"{self.prefix}/status",
                "icon": "mdi:face-recognition",
                "device": device,
            }
            self.client.publish(f"homeassistant/sensor/{self.prefix}_{cam}/config",
                                json.dumps(conf, ensure_ascii=False), retain=True)
            for decision in ("recognized", "unknown", "ambiguous", "no_face"):
                trigger = {
                    "automation_type": "trigger",
                    "topic": f"{self.prefix}/v1/recognitions",
                    "type": decision,
                    "subtype": cam,
                    "payload": decision,
                    "value_template": "{{ value_json.decision if value_json.camera == " + json.dumps(cam, ensure_ascii=False) + " else '' }}",
                    "device": device,
                }
                self.client.publish(
                    f"homeassistant/device_automation/{self.prefix}_{cam}_{decision}/config",
                    json.dumps(trigger, ensure_ascii=False),
                    retain=True,
                )
            # frischen Anwesenheits-Stand publizieren (räumt auch stale retained States nach Neustart auf)
            self._last_presence.pop(cam, None)
            self.present.setdefault(cam, {})
            self._publish_presence(cam)
        for slug, person in self.gallery.persons().items():
            self._publish_person_discovery(slug, person["name"])
            self._publish_person_state(person["name"])
