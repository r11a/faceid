"""Frigate-Events per MQTT verarbeiten und Ergebnisse für HA publizieren.

Pipeline: frigate/events (person) -> Snapshot-Crop -> ArcFace -> Galerie-Match
  - Match + runner-up margin + repeated agreement -> Person publizieren
  - Match < unknown_threshold                     -> unbekannt
  - dazwischen / zu wenig Bestätigungen           -> unsicher, Review-Queue
"""
import json
import hashlib
import logging
import queue
import threading
import time
from collections import deque

import paho.mqtt.client as mqtt
import requests

from .engine import FaceEngine, crop_face
from .hires import upgrade_face
from .decision import DecisionAccumulator, DecisionPolicy
from .quality import measure_face_quality
from .clip_analyzer import ClipAnalyzer

log = logging.getLogger("faceid.mqtt")


class EventProcessor:
    def __init__(
        self, cfg: dict, engine, gallery, frigate, audit=None, *,
        scenario_manager=None, reid=None, dispatcher=None, ai_context=None,
    ):
        self.cfg = cfg
        self.engine = engine
        self.gallery = gallery
        self.frigate = frigate
        self.audit = audit
        self.scenario_manager = scenario_manager
        self.reid = reid
        self.dispatcher = dispatcher
        self.ai_context = ai_context
        self.queue: "queue.Queue[dict]" = queue.Queue(maxsize=200)
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
        self.ignore_thr = float(f.get("ignore_threshold", f.get("match_threshold", 0.5)))
        self.ignore_learning = bool(f.get("ignore_learning", True))
        self.hires_enroll = bool(f.get("hires_enroll", True))
        self.clip_analysis = bool(f.get("clip_analysis", True))
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
        self._update_policy()
        self.clip_analyzer = ClipAnalyzer(
            engine, frigate,
            max_frames=int(f.get("clip_max_frames", 24)),
            max_samples=int(f.get("clip_max_samples", 8)),
            min_face_px=self.min_face_px,
            min_quality=self.min_face_quality,
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
        c.on_connect = self._on_connect
        c.on_message = self._on_message
        c.connect(m["host"], int(m.get("port", 1883)), keepalive=60)
        c.loop_start()
        self.client = c
        self._check_frigate()
        threading.Thread(target=self._worker, daemon=True, name="faceid-worker").start()
        threading.Thread(target=self._finalizer, daemon=True, name="faceid-finalizer").start()
        if self.audit:
            self.audit.recover_running_jobs()
            self._dispatch_pending_jobs()
            threading.Thread(
                target=self._job_dispatcher, daemon=True, name="faceid-job-dispatcher"
            ).start()
        if self.poll_interval > 0:
            threading.Thread(target=self._poller, daemon=True, name="faceid-poller").start()

    def _job_dispatcher(self):
        while True:
            time.sleep(2)
            self._dispatch_pending_jobs()

    def _dispatch_pending_jobs(self):
        if not self.audit:
            return
        for job in self.audit.pending_jobs():
            eid, kind = job["event_id"], job["kind"]
            if eid not in self.events:
                self.events[eid] = self._new_event_state(
                    eid, job["camera"], start_time=job.get("start_ts"),
                    end_time=job.get("end_ts"), recovered=True,
                )
            key = (eid, kind)
            if key in self._queued_jobs:
                continue
            try:
                self.queue.put_nowait({"eid": eid, "kind": kind})
                self._queued_jobs.add(key)
            except queue.Full:
                break

    def _enqueue_job(self, eid: str, kind: str = "snapshot") -> bool:
        if (eid, kind) in self._queued_jobs:
            return True
        if self.audit:
            self.audit.queue_job(eid, kind)
            self._dispatch_pending_jobs()
            return (eid, kind) in self._queued_jobs
        try:
            self.queue.put_nowait({"eid": eid, "kind": kind})
            return True
        except queue.Full:
            return False

    def _check_frigate(self):
        """Beim Start einmal nachsehen, ob Frigate ueberhaupt antwortet.

        Ohne diese Zeile im Log ist "es erkennt nichts" kaum von "es kommt nichts an"
        zu unterscheiden."""
        url = self.cfg["frigate"]["url"].rstrip("/")
        try:
            r = requests.get(f"{url}/api/config", timeout=8)
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

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        log.info("MQTT connected (%s), subscribing to %s/events", reason_code, self.frigate_topic)
        client.subscribe(f"{self.frigate_topic}/events")
        client.publish(f"{self.prefix}/status", "online", retain=True)
        self._publish_discovery()

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload)
        except json.JSONDecodeError:
            return
        after = payload.get("after") or {}
        etype = payload.get("type")
        if after.get("label") != "person":
            return
        cam = after.get("camera", "")
        if self.cameras and cam not in self.cameras:
            return
        eid = after.get("id")
        if not eid:
            return
        if eid not in self.events and self.audit and self.audit.was_finalized(eid):
            return
        if eid not in self._handled:
            self._handled.append(eid)
        self._ensure_discovery(cam)
        st = self.events.get(eid)
        if st is None:
            st = self._new_event_state(
                eid, cam, start_time=after.get("start_time") or time.time()
            )
            self.events[eid] = st
        if etype == "end":
            st["ended"] = True
            st["end_time"] = after.get("end_time") or time.time()
            if self.audit:
                self.audit.mark_ended(eid, st["end_time"])
        if st["done"] or st["attempts"] >= self.max_attempts:
            return
        if after.get("has_snapshot") and time.time() - st["last_try"] >= self.retry_secs:
            st["last_try"] = time.time()
            if not self._enqueue_job(eid, "snapshot"):
                log.warning("queue full, skipping event %s", eid)

    # ---------- Verarbeitung ----------

    def _worker(self):
        while True:
            item = self.queue.get()
            eid = item["eid"]
            kind = item.get("kind", "snapshot")
            try:
                if self.audit:
                    self.audit.mark_job_running(eid, kind)
                if kind == "clip":
                    self._process_clip(eid)
                else:
                    self._process(eid)
                if self.audit:
                    self.audit.complete_job(eid, kind)
            except Exception as e:
                if self.audit:
                    self.audit.retry_job(eid, kind, str(e))
                log.exception("error while handling event %s (%s)", eid, kind)
            finally:
                self._queued_jobs.discard((eid, kind))
                self.queue.task_done()

    def _process_clip(self, eid: str):
        st = self.events.get(eid)
        if st is None or st["done"]:
            return
        st["clip_queued"] = False
        reference = (
            st["best_unknown"]["emb"] if st.get("best_unknown") is not None else None
        )
        samples = self.clip_analyzer.analyze(eid, reference_embedding=reference)
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
        st["attempts"] += 1
        img = self.frigate.snapshot(eid, crop=True)
        if img is None:
            if self.audit:
                self.audit.observation(eid, st["attempts"], "no_snapshot")
            log.info("event %s (%s): no snapshot from Frigate", eid, st["camera"])
            return
        st["context_frame"] = img
        found = self.engine.faces(img)
        measured = [
            (measure_face_quality(
                img, item, min_face_px=self.min_face_px,
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
        self._process_face(eid, st, img, face, quality=quality, source="snapshot")

    def _process_face(self, eid: str, st: dict, img, face, quality=None, source="snapshot"):
        if st["done"]:
            return
        emb = face.normed_embedding
        candidates = self.gallery.match_candidates(emb, limit=2)
        slug, name, score = candidates[0] if candidates else (None, None, 0.0)
        _, runner_up, runner_up_score = (
            candidates[1] if len(candidates) > 1 else (None, None, 0.0)
        )
        ig, _, ignore_group = self.gallery.match_ignored_detail(emb)
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
        face_px = int(min(face.bbox[2] - face.bbox[0], face.bbox[3] - face.bbox[1]))
        if self.audit:
            self.audit.observation(
                eid, st["attempts"], decision.status, person=name, score=float(score),
                runner_up=runner_up, runner_up_score=float(runner_up_score),
                margin=float(decision.margin), ignore_score=float(ig),
                det_score=float(face.det_score), face_px=face_px,
                quality=float(quality.score) if quality else 0.0,
                source=source,
            )

        if decision.status == "ignored":
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
            st["best_score"], st["best_person"] = decision.score, decision.person
            st["done"] = True
            st["final_decision"] = decision
            self._publish_recognition(
                eid, st, decision.person, decision.score, decision=decision
            )
            if self.set_sub_label:
                self.frigate.set_sub_label(eid, decision.person, decision.score)
            if self.reid is not None:
                self.reid.seed(
                    decision.person, st["camera"], st.get("context_frame"),
                    ts=st.get("start_time"),
                )
            if self.audit:
                self.audit.finalize(
                    eid, "recognized", end_ts=st.get("end_time"),
                    person=decision.person, score=decision.score,
                    margin=decision.margin, confirmations=decision.confirmations,
                )
            return

        # Keep the best review image until the event ends. A high but insufficiently
        # confirmed match is ambiguous, never silently promoted or ignored.
        prev = st.get("best_unknown")
        if prev is None or face.det_score > prev["det_score"]:
            st["best_unknown"] = {
                "crop": crop, "emb": emb, "det_score": float(face.det_score),
                "guess": name, "guess_score": float(score), "full": img,
                "decision": st["decision"].pending_status(),
                "runner_up": runner_up, "runner_up_score": float(runner_up_score),
                "margin": float(decision.margin),
                "quality": quality.to_dict() if quality else {},
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
                r = requests.get(f"{url}/api/events",
                                 params={"label": "person", "has_snapshot": 1,
                                         "limit": 50, "after": since - 30},
                                 timeout=10)
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
                if self.cameras and cam not in self.cameras:
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
            for eid in list(self.events.keys()):
                st = self.events[eid]
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
                        log.warning("queue full — clip analysis delayed for %s", eid)
                        continue
                final_status = None
                final_person = None
                final_score = 0.0
                final_margin = 0.0
                confirmations = 0
                if st["best_person"] is None and st["best_unknown"] is not None:
                    u = st["best_unknown"]
                    crop, emb, full = u["crop"], u["emb"], u.get("full")
                    if self.hires_enroll:
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
                        )
                    elif not self.audit.was_finalized(eid):
                        final_status = "no_face"
                        self.audit.finalize(
                            eid, "no_face", end_ts=st.get("end_time")
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
                        final_status = "no_face"
                self._post_event(
                    eid, st, final_status, final_person, final_score,
                    margin=final_margin, confirmations=confirmations,
                )
                self.events.pop(eid, None)

    # ---------- Publish ----------

    def _post_event(
        self, eid: str, st: dict, status: str, person: str | None,
        score: float, *, margin: float = 0.0, confirmations: int = 0,
    ):
        """Publish one enriched final event after all frame/clip evidence is known."""
        if st.get("post_processed"):
            return
        st["post_processed"] = True
        probable_person, probable_score = None, 0.0
        if (
            self.reid is not None
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
        if self.scenario_manager is not None:
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
        }
        if self.dispatcher is not None:
            try:
                self.dispatcher.dispatch(
                    payload, client=self.client, prefix=self.prefix
                )
            except Exception:
                log.exception("event %s: automation dispatch failed", eid)
        if self.ai_context is not None:
            try:
                self.ai_context.submit(
                    eid, st.get("context_frame"),
                    camera=st["camera"], decision=status,
                )
            except Exception:
                log.exception("event %s: AI context enqueue failed", eid)

    def _publish_recognition(
        self, eid: str, st: dict, name: str, score: float,
        decision=None, decision_status: str | None = None,
    ):
        payload = {
            "person": name, "score": round(float(score), 3), "camera": st["camera"],
            "event_id": eid, "ts": time.time(),
            "decision": decision_status or (decision.status if decision else name),
        }
        if decision is not None:
            payload.update({
                "margin": round(float(decision.margin), 3),
                "confirmations": int(decision.confirmations),
                "runner_up": decision.runner_up,
                "runner_up_score": round(float(decision.runner_up_score), 3),
            })
        self.recent.appendleft(payload)
        # faceid/event genau einmal pro (Event, Person) — Score-Verbesserungen lösen keine
        # erneute Meldung aus (sonst mehrere Notifications für dieselbe Sichtung)
        if self.client and st.get("announced") != name:
            st["announced"] = name
            self.client.publish(f"{self.prefix}/event", json.dumps(payload, ensure_ascii=False))
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

    def _frigate_cameras(self) -> set:
        """Kameranamen von Frigate holen — fuer den Fall, dass keine konfiguriert sind."""
        try:
            r = requests.get(f"{self.cfg['frigate']['url'].rstrip('/')}/api/config", timeout=8)
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
                    "topic": f"{self.prefix}/v1/events",
                    "type": decision,
                    "subtype": cam,
                    "payload": decision,
                    "value_template": "{{ value_json.decision }}",
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
