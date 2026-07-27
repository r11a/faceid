"""Persistent, append-only recognition audit backed by SQLite."""
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path


class AuditStore:
    def __init__(self, path: Path, retention_days: int = 90):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()
        self.prune(retention_days)

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

    def set_ground_truth(self, event_id: str, label: str) -> bool:
        with self._lock, self._connection() as con:
            cur = con.execute(
                "UPDATE events SET ground_truth=?, ground_truth_ts=?, updated_ts=? "
                "WHERE event_id=?",
                (label, time.time(), time.time(), event_id),
            )
            return cur.rowcount > 0

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
