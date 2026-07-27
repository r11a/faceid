"""Persistent, append-only recognition audit backed by SQLite."""
import hashlib
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path


class AuditStore:
    def __init__(self, path: Path, retention_days: int = 90):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.evidence_dir = self.path.parent / "audit_images"
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()
        self.prune(retention_days)

    def evidence_path(self, event_id: str) -> Path:
        safe_name = hashlib.sha256(event_id.encode("utf-8")).hexdigest()
        return self.evidence_dir / f"{safe_name}.jpg"

    def save_evidence(self, event_id: str, image) -> Path | None:
        """Persist a compact review image so verification survives Frigate retention."""
        if image is None:
            return None
        temporary = None
        try:
            import cv2

            ok, encoded = cv2.imencode(
                ".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 88]
            )
            if not ok:
                return None
            target = self.evidence_path(event_id)
            temporary = target.with_suffix(".tmp")
            temporary.write_bytes(encoded.tobytes())
            temporary.replace(target)
            return target
        except (OSError, ValueError):
            return None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _connect(self):
        con = sqlite3.connect(self.path, timeout=10)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=FULL")
        return con

    @contextmanager
    def _connection(self):
        con = self._connect()
        try:
            with con:
                yield con
        finally:
            con.close()

    def _init_db(self):
        with self._lock, self._connection() as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    camera TEXT NOT NULL,
                    start_ts REAL,
                    end_ts REAL,
                    status TEXT NOT NULL DEFAULT 'processing',
                    person TEXT,
                    score REAL,
                    margin REAL,
                    confirmations INTEGER NOT NULL DEFAULT 0,
                    created_ts REAL NOT NULL,
                    updated_ts REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    ts REAL NOT NULL,
                    status TEXT NOT NULL,
                    person TEXT,
                    score REAL,
                    runner_up TEXT,
                    runner_up_score REAL,
                    margin REAL,
                    ignore_score REAL,
                    det_score REAL,
                    face_px INTEGER,
                    quality REAL,
                    source TEXT,
                    FOREIGN KEY(event_id) REFERENCES events(event_id)
                );
                CREATE INDEX IF NOT EXISTS observations_event_idx
                    ON observations(event_id, id);
                CREATE TABLE IF NOT EXISTS scenarios (
                    scenario_id TEXT PRIMARY KEY,
                    start_ts REAL NOT NULL,
                    end_ts REAL NOT NULL,
                    status TEXT NOT NULL,
                    person TEXT,
                    cameras TEXT NOT NULL,
                    event_count INTEGER NOT NULL DEFAULT 0,
                    updated_ts REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    event_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    available_ts REAL NOT NULL,
                    last_error TEXT,
                    updated_ts REAL NOT NULL,
                    PRIMARY KEY(event_id, kind)
                );
                CREATE TABLE IF NOT EXISTS review_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL,
                    previous_label TEXT,
                    new_label TEXT,
                    reviewer TEXT NOT NULL,
                    action TEXT NOT NULL DEFAULT 'label',
                    ts REAL NOT NULL,
                    FOREIGN KEY(event_id) REFERENCES events(event_id)
                );
                CREATE INDEX IF NOT EXISTS review_history_event_idx
                    ON review_history(event_id, id DESC);
                """
            )
            self._ensure_column(con, "observations", "quality", "REAL")
            self._ensure_column(con, "observations", "source", "TEXT")
            for column, declaration in (
                ("ground_truth", "TEXT"),
                ("ground_truth_ts", "REAL"),
                ("scenario_id", "TEXT"),
                ("ai_description", "TEXT"),
                ("ai_tags", "TEXT"),
                ("ai_embedding", "TEXT"),
                ("probable_person", "TEXT"),
                ("probable_score", "REAL"),
                ("ground_truth_by", "TEXT"),
            ):
                self._ensure_column(con, "events", column, declaration)

    @staticmethod
    def _ensure_column(con, table: str, column: str, declaration: str):
        existing = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def start_event(self, event_id: str, camera: str, start_ts: float | None):
        now = time.time()
        with self._lock, self._connection() as con:
            con.execute(
                """
                INSERT INTO events(event_id, camera, start_ts, created_ts, updated_ts)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    camera=excluded.camera,
                    start_ts=COALESCE(events.start_ts, excluded.start_ts),
                    updated_ts=excluded.updated_ts
                WHERE events.status='processing'
                """,
                (event_id, camera, start_ts, now, now),
            )

    def was_finalized(self, event_id: str) -> bool:
        with self._lock, self._connection() as con:
            row = con.execute(
                "SELECT status FROM events WHERE event_id=?", (event_id,)
            ).fetchone()
        return bool(row and row[0] != "processing")

    def mark_ended(self, event_id: str, end_ts: float):
        with self._lock, self._connection() as con:
            con.execute(
                "UPDATE events SET end_ts=?, updated_ts=? WHERE event_id=? "
                "AND status='processing'",
                (end_ts, time.time(), event_id),
            )

    def observation(
        self,
        event_id: str,
        attempt: int,
        status: str,
        *,
        person: str | None = None,
        score: float = 0.0,
        runner_up: str | None = None,
        runner_up_score: float = 0.0,
        margin: float = 0.0,
        ignore_score: float = 0.0,
        det_score: float = 0.0,
        face_px: int = 0,
        quality: float = 0.0,
        source: str = "snapshot",
    ):
        with self._lock, self._connection() as con:
            con.execute(
                """
                INSERT INTO observations(
                    event_id, attempt, ts, status, person, score, runner_up,
                    runner_up_score, margin, ignore_score, det_score, face_px,
                    quality, source
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    attempt,
                    time.time(),
                    status,
                    person,
                    score,
                    runner_up,
                    runner_up_score,
                    margin,
                    ignore_score,
                    det_score,
                    face_px,
                    quality,
                    source,
                ),
            )

    def finalize(
        self,
        event_id: str,
        status: str,
        *,
        end_ts: float | None = None,
        person: str | None = None,
        score: float = 0.0,
        margin: float = 0.0,
        confirmations: int = 0,
    ):
        with self._lock, self._connection() as con:
            con.execute(
                """
                UPDATE events SET end_ts=?, status=?, person=?, score=?, margin=?,
                    confirmations=?, updated_ts=?
                WHERE event_id=?
                """,
                (
                    end_ts,
                    status,
                    person,
                    score,
                    margin,
                    confirmations,
                    time.time(),
                    event_id,
                ),
            )

    def prune(self, retention_days: int):
        if retention_days <= 0:
            return
        cutoff = time.time() - retention_days * 86400
        with self._lock, self._connection() as con:
            con.execute(
                """
                DELETE FROM jobs
                WHERE event_id IN (
                    SELECT event_id FROM events
                    WHERE status!='processing' AND updated_ts < ?
                )
                """,
                (cutoff,),
            )
            con.execute(
                """
                DELETE FROM observations
                WHERE event_id IN (
                    SELECT event_id FROM events
                    WHERE status!='processing' AND updated_ts < ?
                )
                """,
                (cutoff,),
            )
            con.execute(
                "DELETE FROM events WHERE status!='processing' AND updated_ts < ?",
                (cutoff,),
            )
            con.execute("DELETE FROM scenarios WHERE end_ts < ?", (cutoff,))
        for image in self.evidence_dir.glob("*.jpg"):
            try:
                if image.stat().st_mtime < cutoff:
                    image.unlink()
            except OSError:
                pass

    def recent(self, limit: int = 100, status: str | None = None):
        sql = """
            SELECT event_id, camera, start_ts, end_ts, status, person, score,
                   margin, confirmations, updated_ts, ground_truth, scenario_id,
                   ai_description, ai_tags, probable_person, probable_score
            FROM events
        """
        params = []
        if status:
            sql += " WHERE status=?"
            params.append(status)
        sql += " ORDER BY updated_ts DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        with self._lock, self._connection() as con:
            con.row_factory = sqlite3.Row
            return [dict(row) for row in con.execute(sql, params).fetchall()]

    def search_events(
        self, *, limit: int = 100, offset: int = 0,
        status: str | None = None, person: str | None = None,
        camera: str | None = None, date_from: float | None = None,
        date_to: float | None = None, query: str | None = None,
    ):
        clauses, params = [], []
        if status:
            clauses.append("status=?")
            params.append(status)
        if person:
            clauses.append(
                "(person=? OR probable_person=? OR ground_truth=?)"
            )
            params.extend((person, person, person))
        if camera:
            clauses.append("camera=?")
            params.append(camera)
        if date_from is not None:
            clauses.append("COALESCE(start_ts, updated_ts)>=?")
            params.append(float(date_from))
        if date_to is not None:
            clauses.append("COALESCE(start_ts, updated_ts)<=?")
            params.append(float(date_to))
        if query:
            like = f"%{query.strip()}%"
            clauses.append(
                "(camera LIKE ? OR person LIKE ? OR probable_person LIKE ? "
                "OR ai_description LIKE ? OR ai_tags LIKE ?)"
            )
            params.extend((like, like, like, like, like))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        columns = """
            event_id, camera, start_ts, end_ts, status, person, score, margin,
            confirmations, updated_ts, ground_truth, ground_truth_ts,
            ground_truth_by, scenario_id, ai_description, ai_tags,
            probable_person, probable_score
        """
        with self._lock, self._connection() as con:
            con.row_factory = sqlite3.Row
            total = con.execute(
                f"SELECT COUNT(*) FROM events{where}", params
            ).fetchone()[0]
            rows = con.execute(
                f"SELECT {columns} FROM events{where} "
                "ORDER BY COALESCE(start_ts, updated_ts) DESC LIMIT ? OFFSET ?",
                [*params, max(1, min(int(limit), 500)), max(0, int(offset))],
            ).fetchall()
            cameras = [
                row[0] for row in con.execute(
                    "SELECT DISTINCT camera FROM events ORDER BY camera"
                ).fetchall()
            ]
        return {"events": [dict(row) for row in rows], "total": total,
                "cameras": cameras}

    def context_events(self, limit: int = 500):
        import json

        with self._lock, self._connection() as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                """
                SELECT event_id, camera, start_ts, end_ts, status, person, score,
                       margin, confirmations, ground_truth, scenario_id,
                       ai_description, ai_tags, ai_embedding, probable_person,
                       probable_score
                FROM events WHERE status!='processing'
                ORDER BY updated_ts DESC LIMIT ?
                """,
                (max(1, min(int(limit), 2000)),),
            ).fetchall()
        out = []
        for raw in rows:
            row = dict(raw)
            try:
                row["ai_tags"] = json.loads(row.get("ai_tags") or "[]")
            except (TypeError, json.JSONDecodeError):
                row["ai_tags"] = []
            try:
                row["_embedding"] = json.loads(row.pop("ai_embedding") or "null")
            except (TypeError, json.JSONDecodeError):
                row["_embedding"] = None
            out.append(row)
        return out

    def event_detail(self, event_id: str):
        with self._lock, self._connection() as con:
            con.row_factory = sqlite3.Row
            event = con.execute(
                "SELECT * FROM events WHERE event_id=?", (event_id,)
            ).fetchone()
            if event is None:
                return None
            observations = con.execute(
                """
                SELECT attempt, ts, status, person, score, runner_up,
                       runner_up_score, margin, ignore_score, det_score, face_px,
                       quality, source
                FROM observations WHERE event_id=? ORDER BY id
                """,
                (event_id,),
            ).fetchall()
            return {
                "event": dict(event),
                "observations": [dict(row) for row in observations],
            }

    def set_ground_truth(
        self, event_id: str, label: str, reviewer: str = "operator",
        action: str = "label",
    ) -> bool:
        now = time.time()
        with self._lock, self._connection() as con:
            previous = con.execute(
                "SELECT ground_truth FROM events WHERE event_id=? "
                "AND status!='processing'", (event_id,)
            ).fetchone()
            if previous is None:
                return False
            cur = con.execute(
                "UPDATE events SET ground_truth=?, ground_truth_ts=?, "
                "ground_truth_by=?, updated_ts=? "
                "WHERE event_id=? AND status!='processing'",
                (label, now, reviewer[:80], now, event_id),
            )
            if cur.rowcount:
                con.execute(
                    "INSERT INTO review_history(event_id, previous_label, "
                    "new_label, reviewer, action, ts) VALUES(?, ?, ?, ?, ?, ?)",
                    (event_id, previous[0], label, reviewer[:80], action, now),
                )
            return cur.rowcount > 0

    def undo_ground_truth(self, event_id: str, reviewer: str = "operator"):
        with self._lock, self._connection() as con:
            row = con.execute(
                "SELECT previous_label, new_label FROM review_history "
                "WHERE event_id=? ORDER BY id DESC LIMIT 1", (event_id,)
            ).fetchone()
        if row is None:
            return None
        previous = row[0]
        if previous is None:
            now = time.time()
            with self._lock, self._connection() as con:
                con.execute(
                    "UPDATE events SET ground_truth=NULL, ground_truth_ts=?, "
                    "ground_truth_by=?, updated_ts=? WHERE event_id=?",
                    (now, reviewer[:80], now, event_id),
                )
                con.execute(
                    "INSERT INTO review_history(event_id, previous_label, "
                    "new_label, reviewer, action, ts) VALUES(?, ?, NULL, ?, ?, ?)",
                    (event_id, row[1], reviewer[:80], "undo", now),
                )
            return ""
        self.set_ground_truth(event_id, previous, reviewer, action="undo")
        return previous

    def person_profile(self, person: str, limit: int = 100):
        result = self.search_events(person=person, limit=limit)
        with self._lock, self._connection() as con:
            con.row_factory = sqlite3.Row
            hourly = con.execute(
                "SELECT CAST(strftime('%H', start_ts, 'unixepoch', 'localtime') "
                "AS INTEGER) hour, COUNT(*) count FROM events "
                "WHERE status='recognized' AND person=? GROUP BY hour ORDER BY hour",
                (person,),
            ).fetchall()
            daily = con.execute(
                "SELECT date(start_ts, 'unixepoch', 'localtime') day, COUNT(*) count "
                "FROM events WHERE status='recognized' AND person=? "
                "AND start_ts>=? GROUP BY day ORDER BY day",
                (person, time.time() - 30 * 86400),
            ).fetchall()
            verified = con.execute(
                "SELECT COUNT(*) total, "
                "SUM(CASE WHEN ground_truth=person THEN 1 ELSE 0 END) correct "
                "FROM events WHERE person=? AND ground_truth IS NOT NULL",
                (person,),
            ).fetchone()
            weak = con.execute(
                "SELECT event_id, camera, start_ts, score, margin, status "
                "FROM events WHERE (person=? OR probable_person=?) "
                "AND (score<0.6 OR status='ambiguous') "
                "ORDER BY start_ts DESC LIMIT 20", (person, person)
            ).fetchall()
        stats = self.person_statistics().get(person, {})
        total = int(verified["total"] or 0)
        correct = int(verified["correct"] or 0)
        return {
            "person": person, "statistics": stats,
            "events": result["events"],
            "hourly": [dict(row) for row in hourly],
            "daily": [dict(row) for row in daily],
            "verified": {"total": total, "correct": correct,
                         "accuracy": correct / total if total else None},
            "weak_events": [dict(row) for row in weak],
        }

    def system_report(self):
        with self._lock, self._connection() as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                """
                SELECT camera, COUNT(*) events,
                  SUM(CASE WHEN status='recognized' THEN 1 ELSE 0 END) recognized,
                  SUM(CASE WHEN status='no_face' THEN 1 ELSE 0 END) no_face,
                  SUM(CASE WHEN status IN ('unknown','ambiguous') THEN 1 ELSE 0 END) review,
                  AVG(CASE WHEN score>0 THEN score END) avg_score
                FROM events WHERE status!='processing' AND start_ts>=?
                GROUP BY camera ORDER BY events DESC
                """, (time.time() - 7 * 86400,)
            ).fetchall()
        cameras = []
        for raw in rows:
            row = dict(raw)
            events = max(1, int(row["events"]))
            row["no_face_rate"] = int(row["no_face"] or 0) / events
            row["review_rate"] = int(row["review"] or 0) / events
            row["avg_score"] = float(row["avg_score"] or 0)
            row["level"] = (
                "warning" if row["no_face_rate"] > 0.55
                or row["review_rate"] > 0.45 else "good"
            )
            cameras.append(row)
        return {"window_days": 7, "cameras": cameras}

    def prune_evidence(self, known_days: int, unknown_days: int) -> int:
        """Apply separate image retention without deleting recognition metadata."""
        now = time.time()
        removed = 0
        with self._lock, self._connection() as con:
            rows = con.execute(
                "SELECT event_id, status, updated_ts FROM events "
                "WHERE status!='processing'"
            ).fetchall()
        for event_id, status, updated_ts in rows:
            days = known_days if status == "recognized" else unknown_days
            if days <= 0 or float(updated_ts or now) >= now - days * 86400:
                continue
            path = self.evidence_path(event_id)
            if path.is_file():
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    pass
        return removed

    def delete_person_history(self, person: str) -> int:
        with self._lock, self._connection() as con:
            event_ids = [
                row[0] for row in con.execute(
                    "SELECT event_id FROM events WHERE person=? "
                    "OR probable_person=? OR ground_truth=?", (person, person, person)
                ).fetchall()
            ]
            for event_id in event_ids:
                con.execute("DELETE FROM review_history WHERE event_id=?", (event_id,))
                con.execute("DELETE FROM observations WHERE event_id=?", (event_id,))
                con.execute("DELETE FROM jobs WHERE event_id=?", (event_id,))
                con.execute("DELETE FROM events WHERE event_id=?", (event_id,))
        for event_id in event_ids:
            self.evidence_path(event_id).unlink(missing_ok=True)
        return len(event_ids)

    def person_statistics(self):
        now = time.time()
        with self._lock, self._connection() as con:
            con.row_factory = sqlite3.Row
            totals = con.execute(
                """
                SELECT person, COUNT(*) AS appearances, AVG(score) AS avg_score,
                       MAX(start_ts) AS last_seen,
                       SUM(CASE WHEN date(start_ts, 'unixepoch', 'localtime')
                                      = date('now', 'localtime')
                                THEN 1 ELSE 0 END) AS today,
                       SUM(CASE WHEN start_ts>=? THEN 1 ELSE 0 END) AS last_7_days,
                       SUM(CASE WHEN start_ts>=? THEN 1 ELSE 0 END) AS last_30_days
                FROM events
                WHERE status='recognized' AND person IS NOT NULL
                GROUP BY person
                """,
                (now - 7 * 86400, now - 30 * 86400),
            ).fetchall()
            result = {}
            for raw in totals:
                row = dict(raw)
                last = con.execute(
                    """
                    SELECT camera, score, event_id FROM events
                    WHERE status='recognized' AND person=?
                    ORDER BY start_ts DESC LIMIT 1
                    """,
                    (row["person"],),
                ).fetchone()
                cameras = con.execute(
                    """
                    SELECT camera, COUNT(*) AS count FROM events
                    WHERE status='recognized' AND person=?
                    GROUP BY camera ORDER BY count DESC, camera
                    """,
                    (row["person"],),
                ).fetchall()
                result[row["person"]] = {
                    **row,
                    "avg_score": round(float(row["avg_score"] or 0), 4),
                    "last_camera": last["camera"] if last else None,
                    "last_score": float(last["score"] or 0) if last else 0.0,
                    "last_event_id": last["event_id"] if last else None,
                    "top_camera": cameras[0]["camera"] if cameras else None,
                    "cameras": [dict(item) for item in cameras],
                }
            return result

    def dashboard_summary(self):
        with self._lock, self._connection() as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                """
                SELECT status, COUNT(*) AS count FROM events
                WHERE date(start_ts, 'unixepoch', 'localtime')
                      = date('now', 'localtime')
                GROUP BY status
                """
            ).fetchall()
        counts = {row["status"]: row["count"] for row in rows}
        return {
            "events_24h": sum(counts.values()),
            "recognized_24h": counts.get("recognized", 0),
            "needs_review_24h": (
                counts.get("unknown", 0) + counts.get("ambiguous", 0)
            ),
            "no_face_24h": counts.get("no_face", 0),
            "processing": counts.get("processing", 0),
        }

    def labeled_events(self):
        with self._lock, self._connection() as con:
            con.row_factory = sqlite3.Row
            events = con.execute(
                "SELECT * FROM events WHERE ground_truth IS NOT NULL ORDER BY start_ts"
            ).fetchall()
            out = []
            for event in events:
                observations = con.execute(
                    """
                    SELECT status, person, score, runner_up, runner_up_score,
                           margin, quality, source
                    FROM observations WHERE event_id=? ORDER BY id
                    """,
                    (event["event_id"],),
                ).fetchall()
                out.append({
                    "event": dict(event),
                    "observations": [dict(row) for row in observations],
                })
            return out

    def update_context(
        self, event_id: str, *, description: str | None = None,
        tags: list[str] | None = None, embedding: list[float] | None = None,
        probable_person: str | None = None, probable_score: float | None = None,
    ):
        import json

        with self._lock, self._connection() as con:
            con.execute(
                """
                UPDATE events SET
                    ai_description=COALESCE(?, ai_description),
                    ai_tags=COALESCE(?, ai_tags),
                    ai_embedding=COALESCE(?, ai_embedding),
                    probable_person=COALESCE(?, probable_person),
                    probable_score=COALESCE(?, probable_score),
                    updated_ts=?
                WHERE event_id=?
                """,
                (
                    description,
                    json.dumps(tags, ensure_ascii=False) if tags is not None else None,
                    json.dumps(embedding) if embedding is not None else None,
                    probable_person,
                    probable_score,
                    time.time(),
                    event_id,
                ),
            )

    def scenario_candidates(self, after_ts: float, limit: int = 20):
        with self._lock, self._connection() as con:
            con.row_factory = sqlite3.Row
            return [
                dict(row) for row in con.execute(
                    "SELECT * FROM scenarios WHERE end_ts>=? "
                    "ORDER BY end_ts DESC LIMIT ?",
                    (after_ts, limit),
                ).fetchall()
            ]

    def save_scenario(self, scenario: dict):
        with self._lock, self._connection() as con:
            con.execute(
                """
                INSERT INTO scenarios(
                    scenario_id, start_ts, end_ts, status, person, cameras,
                    event_count, updated_ts
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(scenario_id) DO UPDATE SET
                    start_ts=excluded.start_ts, end_ts=excluded.end_ts,
                    status=excluded.status, person=excluded.person,
                    cameras=excluded.cameras, event_count=excluded.event_count,
                    updated_ts=excluded.updated_ts
                """,
                (
                    scenario["scenario_id"], scenario["start_ts"], scenario["end_ts"],
                    scenario["status"], scenario.get("person"), scenario["cameras"],
                    scenario["event_count"], time.time(),
                ),
            )

    def attach_scenario(self, event_id: str, scenario_id: str):
        with self._lock, self._connection() as con:
            con.execute(
                "UPDATE events SET scenario_id=?, updated_ts=? WHERE event_id=?",
                (scenario_id, time.time(), event_id),
            )

    def recent_scenarios(self, limit: int = 100):
        with self._lock, self._connection() as con:
            con.row_factory = sqlite3.Row
            return [
                dict(row) for row in con.execute(
                    "SELECT * FROM scenarios ORDER BY end_ts DESC LIMIT ?",
                    (max(1, min(limit, 500)),),
                ).fetchall()
            ]

    def queue_job(self, event_id: str, kind: str):
        now = time.time()
        with self._lock, self._connection() as con:
            con.execute(
                """
                INSERT INTO jobs(event_id, kind, status, available_ts, updated_ts)
                VALUES(?, ?, 'pending', ?, ?)
                ON CONFLICT(event_id, kind) DO UPDATE SET
                    status='pending', available_ts=excluded.available_ts,
                    last_error=NULL, updated_ts=excluded.updated_ts
                WHERE jobs.status IN ('done', 'failed')
                """,
                (event_id, kind, now, now),
            )

    def mark_job_running(self, event_id: str, kind: str):
        with self._lock, self._connection() as con:
            con.execute(
                """
                UPDATE jobs SET status='running', attempts=attempts+1, updated_ts=?
                WHERE event_id=? AND kind=?
                """,
                (time.time(), event_id, kind),
            )

    def complete_job(self, event_id: str, kind: str):
        with self._lock, self._connection() as con:
            con.execute(
                "UPDATE jobs SET status='done', updated_ts=? WHERE event_id=? AND kind=?",
                (time.time(), event_id, kind),
            )

    def retry_job(self, event_id: str, kind: str, error: str, delay: float = 5.0):
        now = time.time()
        with self._lock, self._connection() as con:
            row = con.execute(
                "SELECT attempts FROM jobs WHERE event_id=? AND kind=?",
                (event_id, kind),
            ).fetchone()
            attempts = int(row[0]) if row else 1
            status = "failed" if attempts >= 5 else "pending"
            con.execute(
                """
                UPDATE jobs SET status=?, available_ts=?, last_error=?, updated_ts=?
                WHERE event_id=? AND kind=?
                """,
                (
                    status, now + delay * max(1, attempts), error[:500], now,
                    event_id, kind,
                ),
            )

    def pending_jobs(self, limit: int = 100):
        now = time.time()
        with self._lock, self._connection() as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                """
                SELECT j.event_id, j.kind, e.camera, e.start_ts, e.end_ts
                FROM jobs j JOIN events e ON e.event_id=j.event_id
                WHERE j.status='pending' AND j.available_ts <= ?
                  AND e.status='processing'
                ORDER BY j.updated_ts LIMIT ?
                """,
                (now, max(1, min(limit, 500))),
            ).fetchall()
            return [dict(row) for row in rows]

    def recover_running_jobs(self):
        with self._lock, self._connection() as con:
            con.execute(
                "UPDATE jobs SET status='pending', available_ts=?, updated_ts=? "
                "WHERE status='running'",
                (time.time(), time.time()),
            )
