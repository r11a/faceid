"""Review-UI + JSON-API: Personen verwalten, Unknown-Cluster zuordnen, letzte Erkennungen."""
import base64
import io
import json
import logging
import secrets
import tarfile
import threading
import time
from pathlib import Path

import cv2
import numpy as np
from fastapi import FastAPI, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from . import logbuffer, VERSION
from .engine import FaceEngine, crop_face, find_face_padded
from .gallery import _atomic_write_json
from .backup_util import build_backup_gz, write_backup_file, prune_backups
from .calibration import UNKNOWN_LABEL, build_calibration_report
from .gallery_coach import gallery_coach_report
from .quality import measure_face_quality
from pathlib import Path as _P

log = logging.getLogger("faceid.web")


class AssignBody(BaseModel):
    ids: list[str]
    person: str  # Slug einer bestehenden ODER Name einer neuen Person


class NameBody(BaseModel):
    name: str


def build_app(cfg, engine, gallery, processor, data_dir: Path, static_dir: Path) -> FastAPI:
    app = FastAPI(title="FaceID")

    # Optionales HTTP Basic Auth (config: faceid.auth.user/password). Als Middleware,
    # damit auch der /data-Static-Mount (Gesichtsbilder!) geschützt ist.
    auth = cfg["faceid"].get("auth") or {}
    if auth.get("user") and auth.get("password"):
        expected = base64.b64encode(f"{auth['user']}:{auth['password']}".encode()).decode()
        log.info("HTTP Basic Auth aktiv (User %s)", auth["user"])

        @app.middleware("http")
        async def basic_auth(request, call_next):
            header = request.headers.get("authorization", "")
            if header.startswith("Basic ") and secrets.compare_digest(header[6:], expected):
                return await call_next(request)
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="FaceID"'})

    @app.get("/media/{kind}/{item_path:path}")
    def media(kind: str, item_path: str):
        """Serve only gallery JPEGs; never expose embeddings, settings or backups."""
        if kind not in {"persons", "unknowns", "ignored"}:
            raise HTTPException(404, "Unknown media collection")
        base = (data_dir / kind).resolve()
        target = (base / item_path).resolve()
        if target.suffix.lower() not in {".jpg", ".jpeg"} or base not in target.parents:
            raise HTTPException(404, "Unknown media")
        if not target.is_file():
            raise HTTPException(404, "Unknown media")
        return FileResponse(target)

    def _index_response():
        """Return HTML bytes directly so neither Starlette nor ingress can reuse an ETag."""
        return Response(
            content=(static_dir / "index.html").read_bytes(),
            media_type="text/html; charset=utf-8",
            headers={
                "Cache-Control": (
                    "no-store, no-cache, must-revalidate, proxy-revalidate, "
                    "max-age=0, s-maxage=0"
                ),
                "Pragma": "no-cache",
                "Expires": "0",
                "Surrogate-Control": "no-store",
                "X-FaceID-UI-Version": VERSION,
            },
        )

    @app.get("/")
    def index():
        return _index_response()

    @app.get("/ui-3.1.1")
    def versioned_index():
        return _index_response()

    @app.get("/api/persons")
    def persons():
        return gallery.persons()

    @app.post("/api/persons")
    def create_person(body: NameBody):
        return {"slug": gallery.create_person(body.name)}

    @app.delete("/api/persons/{slug}")
    def delete_person(slug: str, purge_history: bool = False):
        person = gallery.persons().get(slug)
        if person is None:
            raise HTTPException(404, "Unknown person")
        gallery.delete_person(slug)
        removed_events = (
            processor.audit.delete_person_history(person["name"])
            if purge_history and processor.audit else 0
        )
        return {"ok": True, "removed_events": removed_events}

    class FavBody(BaseModel):
        favorite: bool

    @app.post("/api/persons/{slug}/favorite")
    def set_favorite(slug: str, body: FavBody):
        return {"ok": gallery.set_favorite(slug, body.favorite)}

    @app.post("/api/persons/{slug}/trimmed/{fname}/restore")
    def restore_trimmed(slug: str, fname: str):
        return {"ok": gallery.restore_trimmed(slug, fname)}

    @app.delete("/api/persons/{slug}/trimmed/{fname}")
    def delete_trimmed(slug: str, fname: str):
        gallery.delete_trimmed(slug, fname)
        return {"ok": True}

    @app.post("/api/persons/{slug}/trimmed/clear")
    def clear_trimmed(slug: str):
        return {"cleared": gallery.clear_trimmed(slug)}

    @app.post("/api/deduplicate")
    def deduplicate(body: dict = None):
        b = body or {}
        thr = float(b.get("threshold", cfg["faceid"].get("dedupe_threshold", 0.65)))
        dry = bool(b.get("dry_run", False))
        # zuerst echte Bild-Dubletten (identisches Foto), dann aehnliche Gesichter
        pix = gallery.deduplicate_pixels_all(dry_run=dry)
        emb = gallery.deduplicate_all(thr, dry_run=dry)
        key = "would_remove" if dry else "moved"
        return {key: pix + emb, "same_image": pix, "similar_face": emb, "threshold": thr}

    @app.delete("/api/persons/{slug}/faces/{fname}")
    def delete_face(slug: str, fname: str):
        gallery.delete_face(slug, fname)
        return {"ok": True}

    @app.post("/api/persons/{slug}/faces/{fname}/unassign")
    def unassign_face(slug: str, fname: str):
        ok = gallery.unassign_face(slug, fname)
        if ok:
            gallery.refresh_guesses()
        return {"ok": ok}

    @app.post("/api/persons/{slug}/ignore")
    def ignore_person(slug: str):
        """Person komplett auf die Ignore-Liste setzen (alle Bilder werden Negativ-Anker)."""
        n = gallery.ignore_person(slug)
        if n:
            gallery.refresh_guesses()
        return {"ignored_faces": n}

    @app.post("/api/persons/{slug}/photos")
    async def upload_photos(slug: str, files: list[UploadFile]):
        """Fotos (z. B. aus der Foto-Library) hochladen: Gesicht extrahieren + einlernen."""
        if slug not in gallery.persons():
            raise HTTPException(404, "Unknown person")
        added, skipped = 0, []
        for uf in files:
            raw = await uf.read()
            img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                skipped.append(f"{uf.filename}: not an image")
                continue
            if max(img.shape[:2]) > 2000:  # Foto-Library-Bilder einkürzen, Detection reicht so
                s = 2000 / max(img.shape[:2])
                img = cv2.resize(img, None, fx=s, fy=s)
            face, img = find_face_padded(engine, img, min_px=60)
            if face is None:
                skipped.append(f"{uf.filename}: no face found")
                continue
            gallery.add_face(slug, crop_face(img, face.bbox), face.normed_embedding,
                             source={"camera": "upload"})
            added += 1
        return {"added": added, "skipped": skipped}

    @app.get("/api/unknowns")
    def unknowns():
        clusters = gallery.unknown_clusters(eps=float(cfg["faceid"].get("cluster_eps", 0.45)))
        frigate_url = cfg["frigate"]["url"].rstrip("/")
        for c in clusters:
            for u in c:
                if u.pop("has_full", False):
                    u["full_url"] = f"media/unknowns/{u['id']}_full.jpg"
                elif u.get("event_id"):
                    # Backfill-Bestand: Vollbild live aus Frigate (solange Event-Retention reicht)
                    u["full_url"] = f"{frigate_url}/api/events/{u['event_id']}/snapshot.jpg"
        return JSONResponse(clusters)

    @app.post("/api/unknowns/assign")
    def assign(body: AssignBody):
        persons_now = gallery.persons()
        slug = body.person if body.person in persons_now else gallery.create_person(body.person)
        name = gallery.persons()[slug]["name"]
        n = 0
        for uid in body.ids:
            jf = gallery.unknown_dir / f"{uid}.json"
            meta = {}
            if jf.exists():
                try:
                    meta = json.loads(jf.read_text())
                except (json.JSONDecodeError, OSError):
                    pass
            if gallery.assign_unknown(uid, slug):
                n += 1
                # Zuordnung ans Original-Event zurückspielen (Mensch bestätigt -> Score 1.0)
                if processor.set_sub_label and meta.get("event_id"):
                    processor.frigate.set_sub_label(meta["event_id"], name, 1.0)
        gallery.refresh_guesses()
        return {"assigned": n, "slug": slug}

    @app.post("/api/unknowns/auto_assign")
    def auto_assign():
        """Alle Unknowns mit Galerie-Match >= match_threshold der vorgeschlagenen Person zuordnen."""
        thr = float(cfg["faceid"].get("match_threshold", 0.5))
        assigned: dict[str, int] = {}
        for it in gallery.unknowns():
            slug, name, score = gallery.match(it["embedding"])
            if slug and score >= thr and gallery.assign_unknown(it["id"], slug):
                assigned[name] = assigned.get(name, 0) + 1
                if processor.set_sub_label and it.get("event_id"):
                    processor.frigate.set_sub_label(it["event_id"], name, score)
        gallery.refresh_guesses()
        return {"assigned": assigned, "total": sum(assigned.values())}

    @app.post("/api/unknowns/ignore")
    def ignore(body: AssignBody):
        """Gesichter auf die Ignore-Liste: nie mehr melden, zuordnen oder vorlegen.
        Alle Gesichter einer Aktion landen in derselben Gruppe."""
        import time as _t
        gid = f"g{int(_t.time() * 1000)}"
        n = sum(1 for uid in body.ids if gallery.ignore_unknown(uid, group=gid))
        return {"ignored": n}

    class MoveBody(BaseModel):
        ids: list[str]
        group: str

    @app.post("/api/ignored/move")
    def move_ignored(body: MoveBody):
        """Anker in eine andere Gruppe verschieben (auch: Gruppen zusammenlegen)."""
        return {"moved": gallery.set_ignored_group(body.ids, body.group)}

    @app.post("/api/ignored/assign")
    def assign_ignored(body: AssignBody):
        """Falsch ignorierte Gesichter direkt einer echten Person zuordnen."""
        persons_now = gallery.persons()
        slug = body.person if body.person in persons_now else gallery.create_person(body.person)
        n = gallery.assign_ignored(body.ids, slug)
        if n:
            gallery.refresh_guesses()
        return {"assigned": n, "slug": slug}

    @app.get("/api/ignored")
    def list_ignored():
        return JSONResponse(gallery.ignored_clusters(eps=float(cfg["faceid"].get("cluster_eps", 0.45))))

    @app.post("/api/ignored/restore")
    def restore_ignored(body: AssignBody):
        n = sum(1 for iid in body.ids if gallery.restore_ignored(iid))
        gallery.refresh_guesses()
        return {"restored": n}

    @app.post("/api/ignored/delete")
    def delete_ignored(body: AssignBody):
        for iid in body.ids:
            gallery.delete_ignored(iid)
        return {"ok": True}

    @app.post("/api/unknowns/discard")
    def discard(body: AssignBody):
        for uid in body.ids:
            gallery.discard_unknown(uid)
        return {"ok": True}

    backfill_file = data_dir / "learning_runs.json"
    try:
        backfill_state = json.loads(backfill_file.read_text("utf-8"))
        if not isinstance(backfill_state, dict):
            raise ValueError("not an object")
    except (OSError, ValueError, json.JSONDecodeError):
        backfill_state = {"running": False, "processed": 0, "total": 0,
                          "result": None, "days": 0, "history": []}
    if backfill_state.get("running"):
        backfill_state.update(running=False, status="interrupted",
                              result={"error": "FaceID restarted during this scan; start a new scan."})
    backfill_cancel = threading.Event()

    def save_backfill_state():
        _atomic_write_json(backfill_file, backfill_state, indent=1)

    class BackfillBody(BaseModel):
        days: int = 14

    @app.post("/api/backfill")
    def start_backfill(body: BackfillBody):
        if backfill_state["running"]:
            raise HTTPException(409, "History scan already running")
        days = max(1, min(int(body.days), 60))
        backfill_cancel.clear()
        backfill_state.update(
            running=True, status="running", processed=0, total=0, result=None,
            days=days, run_id=f"learning-{int(time.time())}", started_ts=time.time(),
            ended_ts=None,
        )
        save_backfill_state()

        def progress(i, total):
            if backfill_cancel.is_set():
                raise InterruptedError("Scan cancelled by the operator")
            backfill_state.update(processed=i, total=total)
            if i % 10 == 0 or i == total:
                save_backfill_state()

        def worker():
            from .backfill import run_backfill
            try:
                stats = run_backfill(
                    engine, gallery, processor.frigate, cfg["frigate"]["url"], days=days,
                    tag=bool(cfg["faceid"].get("set_sub_label", False)),
                    match_thr=float(cfg["faceid"].get("match_threshold", 0.5)),
                    unknown_thr=float(cfg["faceid"].get("unknown_threshold", 0.35)),
                    match_margin=float(cfg["faceid"].get("match_margin", 0.08)),
                    min_confirmations=int(cfg["faceid"].get("min_confirmations", 2)),
                    ignore_thr=float(cfg["faceid"].get(
                        "ignore_threshold", cfg["faceid"].get("match_threshold", 0.5))),
                    ignore_margin=float(cfg["faceid"].get("ignore_margin", 0.12)),
                    progress=progress,
                    hires=bool(cfg["faceid"].get("hires_enroll", True)))
                backfill_state["result"] = stats
                backfill_state["status"] = "completed"
            except InterruptedError as e:
                backfill_state["status"] = "cancelled"
                backfill_state["result"] = {"error": str(e)}
            except Exception as e:
                log.exception("history scan failed")
                backfill_state["status"] = "failed"
                backfill_state["result"] = {"error": str(e)}
            finally:
                backfill_state["running"] = False
                backfill_state["ended_ts"] = time.time()
                history = list(backfill_state.get("history") or [])
                history.insert(0, {k: backfill_state.get(k) for k in (
                    "run_id", "status", "days", "processed", "total", "result",
                    "started_ts", "ended_ts",
                )})
                backfill_state["history"] = history[:10]
                save_backfill_state()

        threading.Thread(target=worker, daemon=True, name="faceid-backfill").start()
        return {"started": True, "days": days}

    @app.get("/api/backfill")
    def backfill_status():
        return backfill_state

    @app.post("/api/backfill/cancel")
    def cancel_backfill():
        if not backfill_state.get("running"):
            raise HTTPException(409, "No history scan is running")
        backfill_cancel.set()
        backfill_state["status"] = "cancelling"
        save_backfill_state()
        return {"ok": True, "status": "cancelling"}

    @app.delete("/api/backfill/runs/{run_id}")
    def delete_backfill_run(run_id: str):
        if backfill_state.get("run_id") == run_id and backfill_state.get("running"):
            raise HTTPException(409, "A running scan cannot be deleted")
        history = list(backfill_state.get("history") or [])
        kept = [row for row in history if row.get("run_id") != run_id]
        if len(kept) == len(history):
            raise HTTPException(404, "Learning run was not found")
        backfill_state["history"] = kept
        save_backfill_state()
        return {"ok": True}

    # Live-editierbare Einstellungen (Settings-Tab). Overlay in data/settings.json.
    SETTINGS_SPEC = {
        "match_threshold": (0.2, 0.9),
        "unknown_threshold": (0.1, 0.8),
        "match_margin": (0.0, 0.5),
        "suggest_threshold": (0.1, 0.9),
        "cluster_eps": (0.3, 0.8),
        "ignore_threshold": (0.1, 0.9),
        "ignore_margin": (0.0, 0.5),
        "dedupe_threshold": (0.50, 0.95),
    }
    BACKUP_SPEC = {"hires_enroll": bool, "backup_enabled": bool, "backup_hour": (0, 23), "backup_keep": (1, 90), "backup_dir": str}
    INT_SPEC = {"max_faces_per_person": (5, 100), "trimmed_keep": (0, 100),
                "match_top_k": (1, 10), "min_confirmations": (1, 6)}
    settings_file = data_dir / "settings.json"

    def _apply_settings(updates: dict):
        f = cfg["faceid"]
        f.update(updates)
        # in processor/gallery gecachte Werte live nachziehen
        policy_updates = {}
        if "match_threshold" in updates: policy_updates["match_thr"] = float(updates["match_threshold"])
        if "unknown_threshold" in updates: policy_updates["unknown_thr"] = float(updates["unknown_threshold"])
        if "match_margin" in updates: policy_updates["match_margin"] = float(updates["match_margin"])
        if "ignore_threshold" in updates: policy_updates["ignore_thr"] = float(updates["ignore_threshold"])
        if "ignore_margin" in updates: policy_updates["ignore_margin"] = float(updates["ignore_margin"])
        if "min_confirmations" in updates: policy_updates["min_confirmations"] = int(updates["min_confirmations"])
        if policy_updates:
            processor.update_decision_policy(**policy_updates)
        trimmed = 0
        if "max_faces_per_person" in updates:
            gallery.max_per_person = int(updates["max_faces_per_person"])
            trimmed = gallery.enforce_cap_all()
        if "trimmed_keep" in updates:
            gallery.trimmed_keep = int(updates["trimmed_keep"])
        if "match_top_k" in updates:
            gallery.top_k = max(1, int(updates["match_top_k"]))
        if "dedupe_threshold" in updates:
            gallery.dedupe_threshold = float(updates["dedupe_threshold"])
        if "hires_enroll" in updates:
            processor.hires_enroll = bool(updates["hires_enroll"])
        # settings.json (nur die editierbaren Keys) persistieren
        keys = set(SETTINGS_SPEC) | set(BACKUP_SPEC) | set(INT_SPEC)
        overlay = {}
        if settings_file.exists():
            try: overlay = json.loads(settings_file.read_text())
            except (json.JSONDecodeError, OSError): overlay = {}
        overlay.update({k: v for k, v in updates.items() if k in keys})
        _atomic_write_json(settings_file, overlay, indent=1)
        return trimmed

    @app.get("/api/settings")
    def get_settings():
        f = cfg["faceid"]
        return {
            "thresholds": {k: float(f.get(k, {"match_threshold":0.5,"unknown_threshold":0.35,
                "match_margin":0.08,"suggest_threshold":0.40,"cluster_eps":0.55,
                "ignore_threshold":0.5,"ignore_margin":0.12,"dedupe_threshold":0.65}[k]))
                for k in SETTINGS_SPEC},
            "ranges": {k: v for k, v in SETTINGS_SPEC.items()},
            "backup": {"enabled": bool(f.get("backup_enabled", False)),
                       "hour": int(f.get("backup_hour", 3)),
                       "keep": int(f.get("backup_keep", 7)),
                       "dir": str(f.get("backup_dir") or "")},
            "max_faces_per_person": int(f.get("max_faces_per_person", 40)),
            "trimmed_keep": int(f.get("trimmed_keep", 10)),
            "match_top_k": int(f.get("match_top_k", 3)),
            "min_confirmations": int(f.get("min_confirmations", 2)),
            "hires_enroll": bool(f.get("hires_enroll", True)),
        }

    @app.post("/api/settings")
    def post_settings(body: dict):
        updates = {}
        for k, (lo, hi) in SETTINGS_SPEC.items():
            if k in body:
                try: v = float(body[k])
                except (TypeError, ValueError): raise HTTPException(400, f"{k} not a number")
                updates[k] = min(max(v, lo), hi)
        for k, spec in BACKUP_SPEC.items():
            if k in body:
                if spec is bool: updates[k] = bool(body[k])
                elif spec is str: updates[k] = str(body[k] or "")
                else:
                    lo, hi = spec
                    updates[k] = min(max(int(body[k]), lo), hi)
        for k, (lo, hi) in INT_SPEC.items():
            if k in body:
                try: updates[k] = min(max(int(body[k]), lo), hi)
                except (TypeError, ValueError): raise HTTPException(400, f"{k} not an int")
        effective_match = float(updates.get("match_threshold", cfg["faceid"].get("match_threshold", 0.5)))
        effective_unknown = float(updates.get("unknown_threshold", cfg["faceid"].get("unknown_threshold", 0.35)))
        if effective_unknown >= effective_match:
            raise HTTPException(400, "unknown_threshold must be lower than match_threshold")
        if int(updates.get("min_confirmations", processor.min_confirmations)) > processor.max_attempts:
            raise HTTPException(400, "min_confirmations cannot exceed max_attempts")
        trimmed = _apply_settings(updates)
        return {"ok": True, "applied": updates, "trimmed": trimmed}

    @app.post("/api/backup/now")
    def backup_now():
        f = cfg["faceid"]
        bdir = _P(f.get("backup_dir") or (data_dir / "backups"))
        p = write_backup_file(data_dir, bdir)
        prune_backups(bdir, int(f.get("backup_keep", 7)))
        return {"ok": True, "file": str(p)}

    @app.get("/api/backup")
    def backup():
        """Komplette Galerie (persons + ignored) als tar.gz — die einzige unersetzliche
        Datenquelle. Unknown-Queue und Frigate-Vollbilder werden bewusst ausgelassen."""
        ts = time.strftime("%Y%m%d-%H%M%S")
        return Response(build_backup_gz(data_dir), media_type="application/gzip",
                        headers={"Content-Disposition": f'attachment; filename="faceid-backup-{ts}.tar.gz"'})

    @app.post("/api/restore")
    async def restore(file: UploadFile, merge: bool = False):
        """Backup einspielen. merge=false (Default) ersetzt persons+ignored komplett;
        merge=true fügt nur fehlende Personen/Anker hinzu (bestehende bleiben)."""
        raw = await file.read()
        try:
            tar = tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz")
        except tarfile.TarError:
            raise HTTPException(400, "Not a valid .tar.gz backup")
        members = tar.getmembers()
        allowed_roots = {"persons", "ignored", "body", "system", "manifest.json"}
        for m in members:
            norm = Path(m.name)
            if (m.name.startswith("/") or ".." in norm.parts or
                    (norm.parts and norm.parts[0] not in allowed_roots)):
                raise HTTPException(400, f"Refusing unsafe path in archive: {m.name}")
            if norm.name == "classifier.pkl":
                raise HTTPException(400, "Executable body models are never accepted from backups; restore material and retrain")
        if not merge:
            write_backup_file(data_dir, data_dir / "backups" / "before-restore")
            for sub in ("persons", "ignored", "body"):
                d = data_dir / sub
                if d.exists():
                    for f in d.rglob("*"):
                        if f.is_file():
                            f.unlink()
                    for f in sorted(d.rglob("*"), reverse=True):
                        if f.is_dir():
                            f.rmdir()
        added = 0
        for m in members:
            if not m.isfile():
                continue
            norm = Path(m.name)
            if norm.parts[0] == "manifest.json":
                continue
            if norm.parts[0] == "system":
                if len(norm.parts) != 2 or norm.parts[1] not in {
                    "settings.json", "learning_runs.json", "frigate_sync.json",
                    "camera_profiles.json", "audit.db"
                }:
                    continue
                target = (data_dir / "audit.restore-pending" if norm.parts[1] == "audit.db"
                          else data_dir / norm.parts[1])
            else:
                target = data_dir / m.name
            if merge and target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with tar.extractfile(m) as src:
                target.write_bytes(src.read())
            added += 1
        tar.close()
        gallery.reload()
        return {"restored_files": added, "mode": "merge" if merge else "replace",
                "persons": len(gallery.persons()),
                "restart_required": (data_dir / "audit.restore-pending").is_file(),
                "safety_backup": "data/backups/before-restore"}

    @app.get("/api/logs")
    def logs(limit: int = 300, level: str | None = None):
        """Die letzten Logzeilen — im Container und standalone sonst nur per Terminal
        einsehbar, genau wenn man wissen will warum nichts erkannt wird."""
        buf = logbuffer.buffer()
        if buf is None:
            return {"lines": [], "note": "Log-Puffer nicht aktiv"}
        return {"lines": buf.tail(max(1, min(limit, 500)), level)}

    @app.get("/api/audit")
    def audit(limit: int = 100, status: str | None = None):
        if processor.audit is None:
            return {"events": []}
        return {"events": processor.audit.recent(limit=limit, status=status)}

    @app.get("/api/activity")
    def activity(
        limit: int = 100, offset: int = 0, status: str | None = None,
        person: str | None = None, camera: str | None = None,
        date_from: float | None = None, date_to: float | None = None,
        q: str | None = None,
    ):
        if processor.audit is None:
            return {"events": [], "scenarios": []}
        result = processor.audit.search_events(
            limit=limit, offset=offset, status=status, person=person,
            camera=camera, date_from=date_from, date_to=date_to, query=q,
        )
        result["scenarios"] = processor.audit.recent_scenarios(limit=limit)
        return result

    @app.get("/api/activity/{event_id}/image")
    def activity_image(event_id: str):
        """Proxy and cache a review image without exposing Frigate credentials."""
        if processor.audit is None:
            raise HTTPException(404, "Audit is not available")
        if processor.audit.event_detail(event_id) is None:
            raise HTTPException(404, "Unknown event")
        cached = processor.audit.evidence_path(event_id)
        if not cached.is_file():
            image = processor.frigate.snapshot(event_id, crop=True)
            cached = processor.audit.save_evidence(event_id, image)
        if cached is None or not cached.is_file():
            raise HTTPException(
                404, "The event image is no longer available in Frigate"
            )
        return FileResponse(
            cached, media_type="image/jpeg",
            headers={"Cache-Control": "private, max-age=86400"},
        )

    @app.get("/api/activity/{event_id}/clip/status")
    def activity_clip_status(event_id: str, prepare: bool = False):
        if processor.audit is None or processor.audit.event_detail(event_id) is None:
            raise HTTPException(404, "Unknown event")
        if processor.media_store is None:
            return {"cached": False, "has_clip": None, "legacy_stream": True}
        status = processor.media_store.status(event_id)
        if prepare and not status["cached"]:
            path = processor.media_store.clip_path(event_id)
            status["cached"] = path is not None
            status["ready"] = path is not None
            if path is None:
                raise HTTPException(
                    404,
                    "Frigate has no event clip and no recording could be built for this time window",
                )
        else:
            status["ready"] = status["cached"]
        return status

    @app.get("/api/activity/{event_id}/clip")
    def activity_clip(event_id: str, request: Request, download: bool = False):
        """Serve a bounded local copy so browser byte ranges survive HA ingress."""
        if processor.audit is None or processor.audit.event_detail(event_id) is None:
            raise HTTPException(404, "Unknown event")
        if processor.media_store is not None:
            path = processor.media_store.clip_path(event_id)
            if path is None:
                raise HTTPException(
                    404, "No event clip was found; the recording fallback also failed."
                )
            headers = {"Cache-Control": "private, max-age=300"}
            disposition = "attachment" if download else "inline"
            safe_event_id = "".join(
                char for char in event_id if char.isalnum() or char in "-_"
            )[:80] or "event"
            headers["Content-Disposition"] = (
                f'{disposition}; filename="faceid-{safe_event_id}.mp4"'
            )
            return FileResponse(path, media_type="video/mp4", headers=headers)

        # Compatibility path for callers constructing EventProcessor without a media store.
        headers = {}
        if request.headers.get("range"):
            headers["Range"] = request.headers["range"]
        try:
            upstream = processor.frigate.request(
                "GET", f"/api/events/{event_id}/clip.mp4",
                headers=headers, stream=True, timeout=processor.frigate.timeout * 8,
            )
        except Exception as exc:
            raise HTTPException(502, f"Frigate clip request failed: {exc}") from exc
        if upstream.status_code not in (200, 206):
            upstream.close()
            raise HTTPException(
                404, "No clip is available. Check Frigate recording retention."
            )

        def chunks():
            try:
                yield from upstream.iter_content(chunk_size=1 << 18)
            finally:
                upstream.close()

        response_headers = {"Cache-Control": "private, max-age=300"}
        for name in ("content-range", "accept-ranges", "content-length"):
            if upstream.headers.get(name):
                response_headers[name.title()] = upstream.headers[name]
        return StreamingResponse(
            chunks(), status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "video/mp4"),
            headers=response_headers,
        )

    @app.get("/api/activity/{event_id}/references")
    def activity_references(event_id: str):
        if processor.audit is None:
            raise HTTPException(404, "Audit is not available")
        detail = processor.audit.event_detail(event_id)
        if detail is None:
            raise HTTPException(404, "Unknown event")
        event = detail["event"]
        name = event.get("person") or event.get("probable_person")
        matches = []
        for slug, person in gallery.persons().items():
            if person["name"] == name:
                matches = [
                    {"url": f"media/persons/{slug}/{filename}",
                     "caption": f"דוגמת ייחוס של {name}"}
                    for filename in (person.get("files") or [])[:3]
                ]
                break
        return {"person": name, "references": matches,
                "note": "Reference examples, not additional event frames."}

    @app.get("/api/persons/{slug}/profile")
    def person_profile(
        slug: str, timezone: str | None = None,
        timezone_offset: int | None = None,
    ):
        person = gallery.persons().get(slug)
        if person is None:
            raise HTTPException(404, "Unknown person")
        profile = (
            processor.audit.person_profile(
                person["name"], timezone_name=timezone,
                timezone_offset_minutes=timezone_offset,
            )
            if processor.audit else {"person": person["name"], "events": []}
        )
        profile["gallery"] = {
            "photos": len(person.get("files") or []),
            "sources": person.get("sources") or {},
            "recommendation": (
                "הוסף עוד תמונות מזוויות ומצלמות שונות"
                if len(person.get("files") or []) < 5 else
                "יש מספיק תמונות בסיס; בדוק את האירועים החלשים"
            ),
        }
        return profile

    @app.get("/api/system-report")
    def system_report():
        report = processor.audit.system_report() if processor.audit else {
            "window_days": 7, "cameras": []
        }
        report["frigate"] = processor.frigate.connection_status()
        report["mqtt"] = (
            processor.dispatcher.health()
            if getattr(processor, "dispatcher", None) else {}
        )
        report["advanced"] = {
            "runtime": (processor.runtime_health.report()
                        if getattr(processor, "runtime_health", None) else None),
            "frames": (processor.frame_distributor.report()
                       if getattr(processor, "frame_distributor", None) else None),
            "body": (processor.body_recognition.status()
                     if getattr(processor, "body_recognition", None) else None),
            "vision": (processor.vision_advisor.status()
                       if getattr(processor, "vision_advisor", None) else None),
        }
        return report

    @app.get("/api/frigate-sync")
    def frigate_sync_report():
        service = getattr(processor, "frigate_sync", None)
        if service is None:
            raise HTTPException(503, "Frigate gallery sync is not available")
        try:
            return service.report()
        except Exception as exc:
            raise HTTPException(502, f"Could not read Frigate's face library: {exc}") from exc

    @app.get("/api/frigate-sync/image")
    def frigate_sync_image(person: str, file: str):
        service = getattr(processor, "frigate_sync", None)
        if service is None:
            raise HTTPException(503, "Frigate gallery sync is not available")
        try:
            raw = service.remote_image(person, file)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(404, str(exc)) from exc
        return Response(raw, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=300"})

    @app.post("/api/frigate-sync/import")
    def frigate_sync_import(body: dict):
        service = getattr(processor, "frigate_sync", None)
        if service is None:
            raise HTTPException(503, "Frigate gallery sync is not available")
        return service.import_selected(list(body.get("items") or []))

    @app.post("/api/frigate-sync/export")
    def frigate_sync_export(body: dict):
        service = getattr(processor, "frigate_sync", None)
        if service is None:
            raise HTTPException(503, "Frigate gallery sync is not available")
        return service.export_selected(list(body.get("items") or []))

    @app.post("/api/frigate-sync/dismiss")
    def frigate_sync_dismiss(body: dict):
        service = getattr(processor, "frigate_sync", None)
        if service is None:
            raise HTTPException(503, "Frigate gallery sync is not available")
        try:
            return service.dismiss(str(body.get("direction") or ""), list(body.get("items") or []))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/body")
    def body_materials():
        service = getattr(processor, "body_recognition", None)
        if service is None:
            raise HTTPException(503, "Body recognition is not available")
        return service.materials()

    @app.get("/api/body/material/{sample_id}/image")
    def body_material_image(sample_id: str):
        service = getattr(processor, "body_recognition", None)
        if service is None or not sample_id.isalnum():
            raise HTTPException(404, "Unknown material")
        path = service.pending / f"{sample_id}.jpg"
        if not path.is_file():
            raise HTTPException(404, "Unknown material")
        return FileResponse(path, media_type="image/jpeg",
                            headers={"Cache-Control": "private, max-age=300"})

    @app.post("/api/body/material/{sample_id}/review")
    def body_material_review(sample_id: str, body: dict):
        service = getattr(processor, "body_recognition", None)
        action, person = str(body.get("action") or ""), body.get("person")
        known = {row["name"] for row in gallery.persons().values()}
        if action == "approve" and person not in known:
            raise HTTPException(400, "Choose an existing person")
        if service is None or not service.review(sample_id, action, person):
            raise HTTPException(404, "Material was not found or the action is invalid")
        return {"ok": True}

    @app.post("/api/body/train")
    def body_train():
        service = getattr(processor, "body_recognition", None)
        if service is None:
            raise HTTPException(503, "Body recognition is not available")
        try:
            return service.train()
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/body/from-event/{event_id}")
    def body_from_event(event_id: str, body: dict):
        service = getattr(processor, "body_recognition", None)
        detail = processor.audit.event_detail(event_id) if processor.audit else None
        person = str(body.get("person") or "")
        if service is None or detail is None:
            raise HTTPException(404, "Event or body service is not available")
        if person not in {row["name"] for row in gallery.persons().values()}:
            raise HTTPException(400, "Choose an existing person")
        image = processor.frigate.snapshot(event_id, crop=True)
        if image is None:
            raise HTTPException(404, "Frigate no longer has this event snapshot")
        result = service.add_pending(event_id, image, person,
                                     detail["event"].get("camera") or "", "human-selected")
        if not result.get("added"):
            raise HTTPException(409, "The image is too small, dark or blurred for body learning")
        return result

    @app.post("/api/body/enabled")
    def body_enabled(body: dict):
        service = getattr(processor, "body_recognition", None)
        if service is None:
            raise HTTPException(503, "Body recognition is not available")
        requested = bool(body.get("enabled"))
        if requested and not service.status()["armed"]:
            raise HTTPException(409, "Review material and train the body model before enabling it")
        service.enabled = requested
        cfg["faceid"]["body_enabled"] = requested
        overlay = {}
        if settings_file.exists():
            try: overlay = json.loads(settings_file.read_text())
            except (json.JSONDecodeError, OSError): pass
        overlay["body_enabled"] = requested
        _atomic_write_json(settings_file, overlay, indent=1)
        return service.status()

    @app.post("/api/recognition-test/{event_id}")
    def recognition_test(event_id: str):
        if processor.audit is None:
            raise HTTPException(404, "Audit is not available")
        detail = processor.audit.event_detail(event_id)
        if detail is None:
            raise HTTPException(404, "Unknown event")
        image = processor.frigate.snapshot(event_id, crop=True)
        if image is None:
            cached = processor.audit.evidence_path(event_id)
            image = cv2.imread(str(cached)) if cached.is_file() else None
        event = detail["event"]
        body_result = ({"status": "no-image", "advisory": True} if image is None else
                       processor.body_recognition.predict(image, event_id)
                       if getattr(processor, "body_recognition", None) else {"enabled": False})
        candidates = [name for name in (event.get("person"), body_result.get("candidate")) if name]
        vision = (processor.vision_advisor.inspect(event_id, list(dict.fromkeys(candidates)))
                  if getattr(processor, "vision_advisor", None) else {"enabled": False})
        return {"event_id": event_id,
                "face": {"decision": event.get("status"), "person": event.get("person"),
                         "score": event.get("score"), "margin": event.get("margin"),
                         "authority": "identity"},
                "body": body_result, "vision": vision,
                "policy": "Only the face result or an explicit human review establishes identity."}

    @app.get("/api/recognition-test/{event_id}/grid")
    def recognition_test_grid(event_id: str):
        service = getattr(processor, "vision_advisor", None)
        if service is None:
            raise HTTPException(404, "Vision advisor is not available")
        path = service.candidate_grid(event_id)
        if path is None:
            raise HTTPException(404, "No event frames are available")
        return FileResponse(path, media_type="image/jpeg",
                            headers={"Cache-Control": "private, max-age=300"})

    @app.get("/api/gallery-coach")
    def gallery_coach():
        return gallery_coach_report(gallery)

    @app.post("/api/gallery-coach/set-aside")
    def gallery_coach_set_aside(body: dict):
        slug, file = str(body.get("slug") or ""), str(body.get("file") or "")
        if not gallery.set_aside_face(slug, file, str(body.get("reason") or "")):
            raise HTTPException(404, "Reference image was not found")
        return {"ok": True, "slug": slug, "file": file}

    @app.get("/api/daily-summary")
    def daily_summary():
        rows = (
            processor.audit.search_events(
                limit=500, date_from=time.time() - 86400
            )["events"] if processor.audit else []
        )
        recognized = [row for row in rows if row["status"] == "recognized"]
        people_seen = sorted({
            row["person"] for row in recognized if row.get("person")
        })
        return {
            "events": len(rows), "recognized": len(recognized),
            "needs_review": sum(
                row["status"] in ("unknown", "ambiguous") for row in rows
            ),
            "people": people_seen,
            "summary": (
                f"ב־24 השעות האחרונות היו {len(rows)} אירועים, "
                f"{len(recognized)} זיהויים ו־"
                f"{sum(row['status'] in ('unknown', 'ambiguous') for row in rows)} "
                "אירועים שממתינים לבדיקה."
            ),
            "identity_policy": "AI never decides a person's identity.",
        }

    @app.get("/api/privacy")
    def privacy():
        images = list((data_dir / "audit_images").glob("*.jpg"))
        return {
            "audit_events": (
                processor.audit.search_events(limit=1)["total"]
                if processor.audit else 0
            ),
            "evidence_images": len(images),
            "evidence_bytes": sum(
                image.stat().st_size for image in images if image.is_file()
            ),
            "known_evidence_days": int(
                cfg["faceid"].get("known_evidence_days", 30)
            ),
            "unknown_evidence_days": int(
                cfg["faceid"].get("unknown_evidence_days", 14)
            ),
            "identity_policy": (
                "AI descriptions and search never choose or change identity."
            ),
        }

    class PrivacyBody(BaseModel):
        known_days: int = 30
        unknown_days: int = 14

    @app.post("/api/privacy/prune")
    def privacy_prune(body: PrivacyBody):
        if processor.audit is None:
            raise HTTPException(404, "Audit is not available")
        known = max(1, min(int(body.known_days), 3650))
        unknown = max(1, min(int(body.unknown_days), 3650))
        removed = processor.audit.prune_evidence(known, unknown)
        return {"ok": True, "removed_images": removed}

    @app.get("/api/dashboard")
    def dashboard():
        people = gallery.persons()
        stats = processor.audit.person_statistics() if processor.audit else {}
        cards = []
        for slug, person in people.items():
            person_stats = stats.get(person["name"], {})
            files = person.get("files") or []
            visit_stats = (
                processor.visits.person_statistics(person["name"])
                if getattr(processor, "visits", None) else {}
            )
            cards.append({
                "slug": slug,
                "name": person["name"],
                "photo": f"media/persons/{slug}/{files[0]}" if files else None,
                "reference_photos": int(person.get("count", len(files))),
                "favorite": bool(person.get("favorite")),
                "appearances": int(person_stats.get("appearances", 0)),
                "today": int(person_stats.get("today", 0)),
                "last_7_days": int(person_stats.get("last_7_days", 0)),
                "last_30_days": int(person_stats.get("last_30_days", 0)),
                "avg_score": float(person_stats.get("avg_score", 0)),
                "last_seen": person_stats.get("last_seen"),
                "last_camera": person_stats.get("last_camera"),
                "last_score": float(person_stats.get("last_score", 0)),
                "top_camera": person_stats.get("top_camera"),
                "cameras": person_stats.get("cameras", []),
                "visit_statistics": visit_stats,
            })
        cards.sort(key=lambda item: (
            not item["favorite"], -(item["last_seen"] or 0), item["name"].casefold()
        ))
        return {
            "summary": (
                processor.audit.dashboard_summary() if processor.audit else {}
            ),
            "people": cards,
            "recent": (
                processor.audit.recent(limit=8, status="recognized")
                if processor.audit else []
            ),
        }

    class CameraProfileBody(BaseModel):
        min_face_px: int
        role: str = "observation"

    def _camera_names():
        names = set(getattr(processor, "cameras", set()) or set())
        profiles = getattr(processor, "camera_profiles", None)
        if profiles:
            names.update(profiles.all().keys())
        if processor.audit:
            names.update(item["camera"] for item in processor.audit.system_report()["cameras"])
        names.update(processor.frigate.cameras())
        return sorted(name for name in names if name)

    @app.get("/api/cameras/studio")
    def camera_studio(days: int = 7):
        profiles = getattr(processor, "camera_profiles", None)
        funnels = {
            row["camera"]: row for row in (
                processor.audit.camera_funnels(days) if processor.audit else []
            )
        }
        cameras = []
        for camera in _camera_names():
            profile = profiles.get(camera) if profiles else {
                "camera": camera, "min_face_px": processor.min_face_px,
                "role": "observation",
            }
            samples = (
                processor.audit.camera_samples(camera, 24) if processor.audit else []
            )
            threshold = profile["min_face_px"]
            measurable = [row for row in samples if int(row.get("face_px") or 0) > 0]
            accepted = sum(int(row["face_px"]) >= threshold for row in measurable)
            cameras.append({
                **profile, "funnel": funnels.get(camera, {}), "samples": samples,
                "impact": {
                    "measured": len(measurable), "accepted": accepted,
                    "rejected": len(measurable) - accepted,
                },
            })
        return {"window_days": max(1, min(days, 90)), "cameras": cameras}

    @app.post("/api/cameras/{camera}/profile")
    def save_camera_profile(camera: str, body: CameraProfileBody):
        profiles = getattr(processor, "camera_profiles", None)
        if profiles is None:
            raise HTTPException(503, "Camera profiles are unavailable")
        try:
            return {"ok": True, "profile": profiles.update(
                camera, min_face_px=body.min_face_px, role=body.role
            )}
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/cameras/{camera}/frame")
    def camera_frame(camera: str):
        if camera not in _camera_names():
            raise HTTPException(404, "Unknown camera")
        content = processor.frigate.latest_frame_bytes(camera)
        if not content:
            raise HTTPException(404, "Frigate did not return a current frame")
        return Response(
            content=content, media_type="image/jpeg",
            headers={"Cache-Control": "no-store, max-age=0"},
        )

    @app.get("/api/cameras/{camera}/analyze")
    def analyze_camera_frame(camera: str):
        if camera not in _camera_names():
            raise HTTPException(404, "Unknown camera")
        content = processor.frigate.latest_frame_bytes(camera)
        image = cv2.imdecode(np.frombuffer(content or b"", np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise HTTPException(404, "Frigate did not return a readable frame")
        profile = processor.camera_profiles.get(camera)
        faces = []
        for face in engine.faces(image):
            quality = measure_face_quality(
                image, face, min_face_px=profile["min_face_px"],
                min_quality=processor.min_face_quality,
            )
            faces.append({
                "box": [round(float(value), 1) for value in face.bbox],
                **quality.to_dict(),
            })
        return {
            "camera": camera, "width": int(image.shape[1]),
            "height": int(image.shape[0]), "profile": profile, "faces": faces,
        }

    @app.get("/api/visits")
    def visits(person: str = "", days: int = 30, limit: int = 200):
        service = getattr(processor, "visits", None)
        if service is None:
            return {"visits": [], "camera_roles_configured": False}
        role_profiles = processor.camera_profiles.all()
        return {
            "visits": service.list(person=person or None, days=days, limit=limit),
            "camera_roles_configured": any(
                item.get("role") != "observation" for item in role_profiles.values()
            ),
        }

    @app.get("/api/search")
    def search(q: str = "", limit: int = 50):
        if processor.audit is None:
            return {"events": []}
        service = getattr(processor, "ai_context", None)
        if service is None:
            rows = processor.audit.context_events(limit=limit)
            for row in rows:
                row.pop("_embedding", None)
            return {"events": rows}
        return {"events": service.search(q, limit=max(1, min(limit, 200)))}

    @app.get("/api/audit/{event_id}")
    def audit_detail(event_id: str):
        if processor.audit is None:
            raise HTTPException(404, "Audit is not available")
        detail = processor.audit.event_detail(event_id)
        if detail is None:
            raise HTTPException(404, "Unknown event")
        return detail

    class GroundTruthBody(BaseModel):
        label: str

    @app.post("/api/audit/{event_id}/ground-truth")
    def ground_truth(event_id: str, body: GroundTruthBody, request: Request):
        labels = {person["name"] for person in gallery.persons().values()}
        if body.label != UNKNOWN_LABEL and body.label not in labels:
            raise HTTPException(400, "Ground truth must be a known person or __unknown__")
        if processor.audit is None:
            raise HTTPException(404, "Audit is not available")
        detail = processor.audit.event_detail(event_id)
        if detail is None:
            raise HTTPException(404, "Unknown event")
        if detail["event"]["status"] == "processing":
            raise HTTPException(409, "Wait for the event to finish before labeling it")
        reviewer = (
            request.headers.get("x-remote-user-name")
            or request.headers.get("x-forwarded-user") or "operator"
        )
        if not processor.audit.set_ground_truth(event_id, body.label, reviewer):
            raise HTTPException(404, "Unknown event")
        return {"ok": True, "event_id": event_id, "label": body.label}

    @app.post("/api/audit/{event_id}/undo")
    def undo_ground_truth(event_id: str, request: Request):
        if processor.audit is None:
            raise HTTPException(404, "Audit is not available")
        reviewer = (
            request.headers.get("x-remote-user-name")
            or request.headers.get("x-forwarded-user") or "operator"
        )
        restored = processor.audit.undo_ground_truth(event_id, reviewer)
        if restored is None:
            raise HTTPException(409, "No previous review is available")
        return {"ok": True, "label": restored or None}

    @app.get("/api/calibration")
    def calibration():
        if processor.audit is None:
            return {"ready": False, "sample_warning": "Audit is not available"}
        return build_calibration_report(
            processor.audit.labeled_events(),
            current_threshold=processor.match_thr,
            current_margin=processor.match_margin,
            confirmations=processor.min_confirmations,
            target_far=float(cfg["faceid"].get("calibration_target_far", 0.01)),
        )

    @app.get("/api/health")
    def health():
        # "queue" ist die Review-Queue — das ist es, was der Header zeigt. Die interne
        # Verarbeitungs-Warteschlange steht separat unter "processing".
        jobs = processor.audit.pending_jobs() if processor.audit else []
        return {"status": "ok", "version": VERSION,
                "persons": len(gallery.persons()),
                "queue": len(list((data_dir / "unknowns").glob("*.json"))),
                "processing": processor.queue.qsize(),
                "open_events": len(processor.events),
                "pending_jobs": len(jobs),
                "engine": engine.health() if hasattr(engine, "health") else {},
                "ai": (processor.ai_context.health()
                       if getattr(processor, "ai_context", None) else {"enabled": False}),
                "integrations": (processor.dispatcher.health()
                                 if getattr(processor, "dispatcher", None) else {}),
                "media": (processor.media_store.report()
                          if getattr(processor, "media_store", None) else None),
                "frames": (processor.frame_distributor.report()
                           if getattr(processor, "frame_distributor", None) else None),
                "body": (processor.body_recognition.status()
                         if getattr(processor, "body_recognition", None) else None),
                "vision": (processor.vision_advisor.status()
                           if getattr(processor, "vision_advisor", None) else None),
                "watchdog": (processor.runtime_health.report()
                             if getattr(processor, "runtime_health", None) else None),
                "frigate": {
                    "secure_mode": processor.frigate.secure_mode,
                    "authenticated": bool(processor.frigate.username),
                    "tls_verified": bool(processor.frigate.verify_tls),
                },
                "suggest_threshold": float(cfg["faceid"].get("suggest_threshold", 0.40))}

    return app
