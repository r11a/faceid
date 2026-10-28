"""Persistent recognition alerts; watchlists never grant or deny physical access."""
import json
import logging
import sqlite3
import threading
import time
from typing import Literal
from urllib.parse import quote

from fastapi import HTTPException
from pydantic import BaseModel

log = logging.getLogger("faceid.alerts")


class AlertPolicyBody(BaseModel):
    watchlist: Literal["normal", "white", "black"] | None = None
    notify_on_recognition: bool | None = None


class AlertStore:
    def __init__(self, audit, gallery, processor):
        self.audit, self.gallery, self.processor = audit, gallery, processor
        self._publish_lock = threading.Lock()
        audit._alert_cleanup = self.refresh_sensor
        with audit._lock, audit._connection() as con:
            con.execute("""CREATE TABLE IF NOT EXISTS recognition_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
                camera TEXT NOT NULL, person TEXT, status TEXT NOT NULL,
                watchlist TEXT NOT NULL, score REAL NOT NULL, ts REAL NOT NULL,
                reasons TEXT NOT NULL)""")
            if "notified_ts" not in {row[1] for row in con.execute("PRAGMA table_info(recognition_alerts)")}:
                con.execute("ALTER TABLE recognition_alerts ADD COLUMN notified_ts REAL")
            # Normal recognitions share the cursor, but must never replace or
            # resurrect the retained policy sensor after a privacy purge.
            con.execute("CREATE TABLE IF NOT EXISTS recognition_alert_state (singleton INTEGER PRIMARY KEY CHECK(singleton=1), policy_id INTEGER NOT NULL)")
            con.execute("INSERT OR IGNORE INTO recognition_alert_state SELECT 1, COALESCE((SELECT seq FROM sqlite_sequence WHERE name='recognition_alerts'),0)")

    def record(self, *, event_id, camera, status, person=None, score=0, ts=None):
        if not event_id or status not in {"recognized", "unknown", "ambiguous"}:
            return None
        confirmed = status == "recognized" and bool(person)
        if status == "recognized" and not confirmed:
            return None
        policy = self.gallery.alert_policy(person) if confirmed else {}
        profiles = getattr(self.processor, "camera_profiles", None)
        camera_policy = profiles.get(camera) if profiles else {}
        reasons = []
        if camera_policy.get("critical"):
            reasons.append("critical_camera")
        if confirmed and policy.get("notify_on_recognition"):
            reasons.append("person_notification")
        if confirmed and policy.get("watchlist") == "black":
            reasons.append("blacklist")
        if not reasons and not confirmed:
            return None
        alert = dict(event_id=event_id, camera=camera, person=person if confirmed else None,
                     status=status, watchlist=policy.get("watchlist", "normal"),
                     score=float(score or 0), ts=float(ts or time.time()), reasons=reasons, notified_ts=time.time())
        with self.audit._lock, self.audit._connection() as con:
            row = con.execute("""INSERT INTO recognition_alerts
                (event_id,camera,person,status,watchlist,score,ts,reasons,notified_ts)
                SELECT ?,?,?,?,?,?,?,?,? WHERE NOT EXISTS
                (SELECT 1 FROM recognition_alerts WHERE event_id=?)""", (event_id, camera, alert["person"], status,
                alert["watchlist"], alert["score"], alert["ts"], json.dumps(reasons), alert["notified_ts"], event_id))
            if not row.rowcount:
                return None
            alert["id"] = row.lastrowid
            if reasons:
                con.execute("UPDATE recognition_alert_state SET policy_id=? WHERE singleton=1", (alert["id"],))
        if hasattr(self.audit, "revisions"):
            self.audit.revisions.publish()
        client = getattr(self.processor, "client", None)
        if client and reasons:
            try:
                with self._publish_lock:
                    client.publish(f"{self.processor.prefix}/v1/alerts", json.dumps(alert, ensure_ascii=False), retain=False)
                    self._publish_latest_locked(client)
            except Exception:
                log.exception("Could not publish recognition alert %s", event_id)
        return alert

    def publish_discovery(self, client):
        """Restore the last-state sensor on reconnect without replaying an alert."""
        prefix = self.processor.prefix
        key = f"{prefix}_last_alert"
        config = {
            "name": "FaceID last alert", "unique_id": key, "object_id": key,
            "state_topic": f"{prefix}/v1/alerts/state",
            "value_template": "{{ value_json.id }}",
            "json_attributes_topic": f"{prefix}/v1/alerts/state",
            "availability_topic": f"{prefix}/status", "icon": "mdi:bell-alert",
            "device": {"identifiers": [prefix],
                       "name": prefix.replace("-", " ").title() if prefix != "faceid" else "FaceID",
                       "manufacturer": "Eigenbau", "model": "InsightFace/ArcFace"},
        }
        with self._publish_lock:
            client.publish(f"homeassistant/sensor/{key}/config", json.dumps(config), retain=True)
            self._publish_latest_locked(client)

    def publish_latest(self, client):
        with self._publish_lock:
            self._publish_latest_locked(client)

    def refresh_sensor(self):
        """Refresh retained metadata after cleanup without changing purge success."""
        client = getattr(self.processor, "client", None)
        if client is not None:
            try:
                self.publish_latest(client)
            except Exception:
                log.exception("Could not refresh retained alert state after cleanup")

    def _publish_latest_locked(self, client):
        with self.audit._lock, self.audit._connection() as con:
            con.row_factory = sqlite3.Row
            row = con.execute("SELECT * FROM recognition_alerts WHERE reasons != '[]' ORDER BY id DESC LIMIT 1").fetchone()
            sequence = con.execute("SELECT policy_id FROM recognition_alert_state WHERE singleton=1").fetchone()
            latest_id = sequence[0] if sequence else 0
        # Keep the encounter counter monotonic while removing attributes for a
        # deleted last alert; never resurrect an older person's private details.
        if row and row["id"] == latest_id:
            state = {**dict(row), "reasons": json.loads(row["reasons"])}
        else:
            state = {"id": latest_id, "event_id": None, "camera": None, "person": None,
                     "status": "cleared" if latest_id else "none", "score": 0,
                     "reasons": [], "watchlist": "normal", "ts": None}
        client.publish(f"{self.processor.prefix}/v1/alerts/state", json.dumps(state, ensure_ascii=False), retain=True)

    def list(self, after=None, limit=50, recent=False, include_recognitions=False):
        limit = max(1, min(int(limit), 100))
        with self.audit._lock, self.audit._connection() as con:
            con.row_factory = sqlite3.Row
            latest = con.execute("SELECT COALESCE(MAX(id),0) FROM recognition_alerts").fetchone()[0]
            if after is None and not recent:
                return {"alerts": [], "cursor": latest, "has_more": False}
            condition = "1=1" if include_recognitions else "reasons != '[]'"
            if recent:
                rows = con.execute(f"SELECT * FROM recognition_alerts WHERE {condition} ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
                rows = list(reversed(rows))
            else:
                rows = con.execute(f"SELECT * FROM recognition_alerts WHERE id>? AND {condition} ORDER BY id LIMIT ?", (max(0, after), limit)).fetchall()
            alerts = [{**dict(row), "reasons": json.loads(row["reasons"])} for row in rows]
            for alert in alerts:
                alert["image_url"] = f"api/activity/{quote(alert['event_id'], safe='')}/image"
            cursor = alerts[-1]["id"] if alerts else latest
            return {"alerts": alerts, "cursor": cursor, "has_more": cursor < latest}


def register_alert_routes(app, processor, gallery):
    audit = getattr(processor, "audit", None)
    if getattr(processor, "alerts", None) is None:
        processor.alerts = AlertStore(audit, gallery, processor) if audit and hasattr(audit, "_connection") else None

    @app.get("/api/alerts")
    def alerts(after: int | None = None, limit: int = 50, recent: bool = False, include_recognitions: bool = False):
        if processor.alerts is None:
            return {"alerts": [], "cursor": 0, "has_more": False}
        return processor.alerts.list(after=after, limit=limit, recent=recent, include_recognitions=include_recognitions)

    @app.patch("/api/persons/{slug}/policy")
    def person_policy(slug: str, body: AlertPolicyBody):
        if not gallery.set_alert_policy(slug, **body.model_dump(exclude_none=True)):
            raise HTTPException(404, "Unknown person")
        if audit and hasattr(audit, "revisions"):
            audit.revisions.publish()
        return {"ok": True, "slug": slug, **gallery.persons()[slug]}
