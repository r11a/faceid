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
from .enrollment import choose_enrollment_face
from pathlib import Path as _P

log = logging.getLogger("faceid.web")


class AssignBody(BaseModel):
    ids: list[str]
    person: str  # Slug einer bestehenden ODER Name einer neuen Person


class NameBody(BaseModel):
    name: str


class RenameBody(BaseModel):
    name: str


def build_app(cfg, engine, gallery, processor, data_dir: Path, static_dir: Path) -> FastAPI:
    app = FastAPI(title="FaceID")
    capture_previews: dict[str, tuple[float, bytes]] = {}
    capture_preview_lock = threading.Lock()

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

    access_control = getattr(processor, "access_control", None)
    if access_control is not None:
        @app.middleware("http")
        async def role_guard(request, call_next):
            if not access_control.allowed(
                request.headers, path=request.url.path, method=request.method,
            ):
                return JSONResponse(
                    {"detail": "Your FaceID role does not allow this action"},
                    status_code=403,
                )
            return await call_next(request)

    @app.get("/media/{kind}/{item_path:path}")
    def media(kind: str, item_path: str):
        """Serve only gallery JPEGs; never expose embeddings, settings or backups."""
        if kind not in {"persons", "unknowns", "ignored", "guests"}:
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

    @app.get("/ui-{ui_version}")
    def versioned_index(ui_version: str):
        """Compatibility alias only; Home Assistant ingress always enters at root."""
        return _index_response()

    @app.get("/api/persons")
    def persons():
        return gallery.persons()

    @app.post("/api/persons")
    def create_person(body: NameBody):
        name = body.name.strip()
        if not name:
            raise HTTPException(400, "יש להזין שם")
        if any(person["name"].casefold() == name.casefold() for person in gallery.persons().values()):
            raise HTTPException(409, "כבר קיים אדם בשם הזה")
        return {"slug": gallery.create_person(name)}

    @app.patch("/api/persons/{slug}")
    def rename_person(slug: str, body: RenameBody):
        try:
            if not gallery.rename_person(slug, body.name):
                raise HTTPException(404, "Unknown person")
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"ok": True, "slug": slug, "name": body.name.strip()}

    @app.get("/api/users")
    def users_overview():
        coach = {
            item["slug"]: item for item in gallery_coach_report(gallery)["people"]
        }
        statistics = processor.audit.person_statistics() if processor.audit else {}
        rows = []
        for slug, person in gallery.persons().items():
            report = coach.get(slug, {})
            count = int(person.get("count", 0))
            if count < 3:
                state, label = "new", "צריך עוד תמונות"
            elif report.get("review_count", 0):
                state, label = "review", "כדאי לשפר תמונות"
            else:
                state, label = "ready", "מוכן לזיהוי"
            files = person.get("files") or []
            rows.append({
                "slug": slug, "name": person["name"], "count": count,
                "photo": f"media/persons/{slug}/{files[0]}" if files else None,
                "favorite": bool(person.get("favorite")), "state": state,
                "state_label": label, "advice": report.get("advice", []),
                "statistics": statistics.get(person["name"], {}),
            })
        rows.sort(key=lambda item: (not item["favorite"], item["name"].casefold()))
        return {
            "users": rows, "recommended_photos": {"minimum": 5, "maximum": 10},
            "privacy": "Photos and face templates stay on this FaceID installation.",
        }

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

    def enrollment_preview(image, candidates: list[dict]) -> dict:
        """Return a bounded preview and normalized boxes for an explicit UI choice."""
        height, width = image.shape[:2]
        preview = image
        if max(height, width) > 960:
            scale = 960 / max(height, width)
            preview = cv2.resize(image, None, fx=scale, fy=scale)
        ok, encoded = cv2.imencode(".jpg", preview, [cv2.IMWRITE_JPEG_QUALITY, 82])
        boxes = []
        for item in candidates:
            left, top, right, bottom = item["bbox"]
            boxes.append({
                **item,
                "bbox": [left / width, top / height, right / width, bottom / height],
            })
        return {
            "preview": (
                "data:image/jpeg;base64," + base64.b64encode(encoded).decode("ascii")
                if ok else None
            ),
            "candidates": boxes,
        }

    @app.post("/api/persons/{slug}/photos")
    async def upload_photos(
        slug: str, files: list[UploadFile], face_index: int | None = None,
    ):
        """Fotos (z. B. aus der Foto-Library) hochladen: Gesicht extrahieren + einlernen."""
        if slug not in gallery.persons():
            raise HTTPException(404, "Unknown person")
        added, skipped, details = 0, [], []
        for upload_index, uf in enumerate(files):
            raw = await uf.read()
            img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                skipped.append(f"{uf.filename}: not an image")
                details.append({"file": uf.filename, "upload_index": upload_index, "status": "rejected", "message": "הקובץ אינו תמונה תקינה"})
                continue
            if max(img.shape[:2]) > 2000:  # Foto-Library-Bilder einkürzen, Detection reicht so
                s = 2000 / max(img.shape[:2])
                img = cv2.resize(img, None, fx=s, fy=s)
            faces = list(engine.faces(img))
            if not faces:
                padded_face, padded_image = find_face_padded(engine, img, min_px=60)
                if padded_face is not None:
                    img, faces = padded_image, [padded_face]
            selection = choose_enrollment_face(
                faces, gallery.embeddings(slug), requested_index=face_index,
                min_face_px=60,
            )
            if selection.reason == "invalid_selection":
                skipped.append(f"{uf.filename}: selected face is no longer available")
                details.append({
                    "file": uf.filename, "upload_index": upload_index,
                    "status": "rejected",
                    "message": "הפנים שנבחרו אינן זמינות עוד; נסו לבחור שוב",
                })
                continue
            if selection.reason == "needs_selection":
                skipped.append(f"{uf.filename}: choose one of the detected faces")
                details.append({
                    "file": uf.filename, "upload_index": upload_index,
                    "status": "needs_selection",
                    "message": "נמצאו כמה אנשים — בחרו את הפנים ששייכות לאדם הזה",
                    **enrollment_preview(img, selection.candidates),
                })
                continue
            face = selection.face
            if face is None:
                skipped.append(f"{uf.filename}: no face found")
                details.append({"file": uf.filename, "upload_index": upload_index, "status": "rejected", "message": "לא נמצאו פנים ברורות בגודל 60 פיקסלים לפחות"})
                continue
            quality = measure_face_quality(
                img, face, min_face_px=60,
                min_quality=max(0.32, processor.min_face_quality),
            )
            if not quality.usable:
                if quality.face_px < 60:
                    message = "הפנים קטנות מדי; השתמשו בתמונה קרובה יותר"
                elif quality.sharpness < .25:
                    message = "התמונה מטושטשת; נסו תמונה חדה יותר"
                elif quality.illumination < .25:
                    message = "התאורה קיצונית; נסו אור אחיד על הפנים"
                else:
                    message = "זווית הפנים או איכות התמונה אינן מתאימות"
                skipped.append(f"{uf.filename}: low quality")
                details.append({"file": uf.filename, "upload_index": upload_index, "status": "rejected", "message": message, "quality": quality.to_dict()})
                continue
            gallery.add_face(slug, crop_face(img, face.bbox), face.normed_embedding,
                             source={"camera": "upload"})
            added += 1
            details.append({
                "file": uf.filename, "upload_index": upload_index,
                "status": "added", "message": "התמונה נוספה",
                "selection": selection.reason, "quality": quality.to_dict(),
            })
        count = gallery.persons().get(slug, {}).get("count", 0)
        return {
            "added": added, "skipped": skipped, "details": details, "count": count,
            "ready": count >= 5,
            "next_step": (
                "יש מספיק תמונות בסיסיות; עברו מול מצלמה ובדקו זיהויים"
                if count >= 5 else f"מומלץ להוסיף עוד {5 - count} תמונות מזוויות שונות"
            ),
        }

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

    @app.post("/api/unknowns/resolve")
    def resolve_unknowns(body: AssignBody, request: Request):
        """Human-confirm queue events without turning every crop into gallery data."""
        persons_now = gallery.persons()
        if body.person in persons_now:
            name = persons_now[body.person]["name"]
        else:
            match = next((person["name"] for person in persons_now.values()
                          if person["name"] == body.person), None)
            if match is None:
                raise HTTPException(400, "Choose an existing person")
            name = match
        reviewer = (request.headers.get("x-remote-user-name")
                    or request.headers.get("x-forwarded-user") or "operator")
        resolved = labeled = 0
        for uid in body.ids[:1000]:
            jf = gallery.unknown_dir / f"{uid}.json"
            try:
                meta = json.loads(jf.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            event_id = str(meta.get("event_id") or "")
            if event_id and processor.audit is not None:
                labeled += int(processor.audit.set_ground_truth(
                    event_id, name, reviewer, action="review_queue_resolve"
                ))
            if processor.set_sub_label and event_id:
                processor.frigate.set_sub_label(event_id, name, 1.0)
            gallery.discard_unknown(uid)
            resolved += 1
        return {"resolved": resolved, "labeled": labeled, "person": name,
                "gallery_photos_added": 0}

    @app.post("/api/unknowns/maintenance")
    def maintain_unknowns():
        return gallery.prune_unknown_queue()

    @app.get("/api/unknowns/policy")
    def unknown_queue_policy():
        return {
            "max_total": gallery.review_queue_max_total,
            "max_per_identity": gallery.review_queue_max_per_cluster,
            "retention_days": gallery.review_queue_retention_days,
            "dedupe_days": gallery.review_queue_dedupe_days,
            "evidence_max_total": (
                processor.audit.evidence_known_max
                + processor.audit.evidence_unknown_max
                if processor.audit is not None else 0
            ),
        }

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
                    hires=bool(cfg["faceid"].get("hires_enroll", True)),
                    camera_enabled=processor.camera_enabled)
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
        allowed_roots = {"persons", "ignored", "body", "guests", "system", "manifest.json"}
        for m in members:
            norm = Path(m.name)
            if (m.name.startswith("/") or ".." in norm.parts or
                    (norm.parts and norm.parts[0] not in allowed_roots)):
                raise HTTPException(400, f"Refusing unsafe path in archive: {m.name}")
            if norm.name == "classifier.pkl":
                raise HTTPException(400, "Executable body models are never accepted from backups; restore material and retrain")
        if not merge:
            write_backup_file(data_dir, data_dir / "backups" / "before-restore")
            for sub in ("persons", "ignored", "body", "guests"):
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
                    "camera_profiles.json", "access_control.json", "guest_access.json",
                    "site_map.json", "schema.json",
                    "audit.db"
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
        q: str | None = None, include_presence_updates: bool = False,
    ):
        if processor.audit is None:
            return {"events": [], "scenarios": []}
        result = processor.audit.search_events(
            limit=limit, offset=offset, status=status, person=person,
            camera=camera, date_from=date_from, date_to=date_to, query=q,
            include_presence_updates=include_presence_updates,
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

    @app.get("/api/session")
    def session(request: Request):
        service = getattr(processor, "access_control", None)
        return service.session(request.headers) if service else {
            "id": "local", "name": "Home Assistant", "role": "admin",
            "tabs": ["*"], "enforced": False,
        }

    @app.get("/api/access-policy")
    def access_policy():
        service = getattr(processor, "access_control", None)
        return service.policy() if service else {"enabled": False, "roles": {}}

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
        enabled: bool | None = None
        role: str = "observation"
        mode: str = "standard"
        night_min_face_px: int | None = None
        burst_frames: int = 8
        high_resolution: bool = False
        require_second_factor: bool = True
        liveness_mode: str | None = None
        roi: list[float] | None = None

    def _camera_names():
        names = set(getattr(processor, "cameras", set()) or set())
        profiles = getattr(processor, "camera_profiles", None)
        if profiles:
            names.update(profiles.all().keys())
        if processor.audit:
            names.update(item["camera"] for item in processor.audit.system_report()["cameras"])
        names.update(processor.frigate.cameras())
        return sorted(name for name in names if name)

    @app.get("/api/guests")
    def guests():
        service = getattr(processor, "guest_access", None)
        if service is None:
            raise HTTPException(503, "Guest access is unavailable")
        return {
            "guests": service.list(), "history": service.history(50),
            "threshold": service.threshold, "margin": service.margin,
            "safety": "A face match creates eligibility only. Liveness and a second factor are required before entry is authorized.",
        }

    @app.post("/api/guests")
    async def create_guest(name: str, valid_from: float, valid_until: float,
                           max_entries: int = 1, cameras: str = "", file: UploadFile = None):
        service = getattr(processor, "guest_access", None)
        if service is None:
            raise HTTPException(503, "Guest access is unavailable")
        if file is None:
            raise HTTPException(400, "A clear guest photo is required")
        raw = await file.read()
        if len(raw) > 15_000_000:
            raise HTTPException(413, "Photo is larger than 15 MB")
        image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise HTTPException(400, "The uploaded file is not a readable image")
        if max(image.shape[:2]) > 2400:
            scale = 2400 / max(image.shape[:2]); image = cv2.resize(image, None, fx=scale, fy=scale)
        face, image = find_face_padded(engine, image, min_px=100)
        if face is None:
            raise HTTPException(400, "No clear face of at least 100 pixels was found")
        quality = measure_face_quality(image, face, min_face_px=100, min_quality=max(.4, processor.min_face_quality))
        if not quality.usable:
            raise HTTPException(400, "The face is not sharp, bright or frontal enough for temporary access")
        selected = [item for item in cameras.split(",") if item]
        unknown = set(selected) - set(_camera_names())
        if unknown:
            raise HTTPException(400, f"Unknown cameras: {', '.join(sorted(unknown))}")
        try:
            guest = service.create(
                name=name, valid_from=valid_from, valid_until=valid_until,
                max_entries=max_entries, allowed_cameras=selected,
                crop=crop_face(image, face.bbox), embedding=face.normed_embedding,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"ok": True, "guest": guest, "quality": quality.to_dict()}

    @app.post("/api/guests/{guest_id}/revoke")
    def revoke_guest(guest_id: str):
        try:
            return {"ok": True, "guest": processor.guest_access.revoke(guest_id)}
        except KeyError as exc:
            raise HTTPException(404, "Unknown guest") from exc

    @app.delete("/api/guests/{guest_id}")
    def delete_guest(guest_id: str):
        try:
            processor.guest_access.delete(guest_id)
        except KeyError as exc:
            raise HTTPException(404, "Unknown guest") from exc
        return {"ok": True}

    @app.post("/api/guests/{guest_id}/authorize")
    def authorize_guest(guest_id: str, body: dict):
        event_id = str(body.get("event_id") or "")
        detail = processor.audit.event_detail(event_id) if processor.audit and event_id else None
        pending = next((item for item in processor.guest_access.history(1000)
                        if item.get("guest_id") == guest_id and item.get("event_id") == event_id), None)
        guest = next((item for item in processor.guest_access.list() if item["id"] == guest_id), None)
        if not detail or not pending or not guest:
            raise HTTPException(400, "No matching server-verified guest recognition was found")
        event = detail["event"]
        if event.get("status") != "recognized" or event.get("person") != guest["name"]:
            raise HTTPException(400, "The event is not a finalized recognition for this guest")
        try:
            return processor.guest_access.evaluate(
                guest_id, camera=str(event.get("camera") or ""),
                score=float(event.get("score") or 0),
                runner_up_score=max(0.0, float(event.get("score") or 0) - float(event.get("margin") or 0)),
                liveness_confirmed=event.get("liveness_status") == "live",
                second_factor=bool(body.get("second_factor")),
                event_id=event_id,
            )
        except KeyError as exc:
            raise HTTPException(404, "Unknown guest") from exc

    @app.get("/api/site-map")
    def site_map(days: int = 7):
        service = getattr(processor, "site_intelligence", None)
        if service is None:
            raise HTTPException(503, "Site intelligence is unavailable")
        cameras = _camera_names()
        visits = processor.visits.list(days=max(1, min(days, 30)), limit=50)
        latest = {}
        for visit in visits:
            latest.setdefault(visit["person"], visit)
        return {
            "map": service.map(cameras),
            "analytics": service.analytics(cameras=cameras, days=days),
            "people": [{
                "person": item["person"], "camera": item["last_camera"],
                "last_seen": item["end_ts"], "open": item["open"],
                "route": item["route"], "timeline": item.get("timeline", []),
            } for item in latest.values()],
        }

    @app.post("/api/site-map")
    def save_site_map(body: dict):
        service = getattr(processor, "site_intelligence", None)
        if service is None:
            raise HTTPException(503, "Site intelligence is unavailable")
        try:
            updated = service.update(body, _camera_names())
            graph = {camera: set() for camera in _camera_names()}
            for left, right in updated.get("links", []):
                graph.setdefault(left, set()).add(right)
                graph.setdefault(right, set()).add(left)
            if getattr(processor, "scenario_manager", None) is not None:
                processor.scenario_manager.camera_graph = graph
            if getattr(processor, "reid", None) is not None:
                processor.reid.camera_graph = graph
            return {"ok": True, "map": updated}
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc

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
            profile["enabled"] = processor.camera_enabled(camera)
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
                camera, min_face_px=body.min_face_px, role=body.role,
                mode=body.mode, night_min_face_px=body.night_min_face_px,
                burst_frames=body.burst_frames,
                high_resolution=body.high_resolution,
                require_second_factor=body.require_second_factor,
                liveness_mode=body.liveness_mode,
                roi=body.roi,
                enabled=body.enabled,
            )}
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    class CameraEnabledBody(BaseModel):
        enabled: bool

    @app.post("/api/cameras/{camera}/enabled")
    def set_camera_enabled(camera: str, body: CameraEnabledBody):
        if camera not in _camera_names():
            raise HTTPException(404, "Unknown camera")
        try:
            profile = processor.set_camera_enabled(camera, body.enabled)
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(503, str(exc)) from exc
        profile["enabled"] = processor.camera_enabled(camera)
        return {
            "ok": True,
            "camera": camera,
            "enabled": profile["enabled"],
            "profile": profile,
        }

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

    @app.get("/api/intercom")
    def intercom_overview():
        profiles = getattr(processor, "camera_profiles", None)
        rows = []
        for camera in _camera_names():
            profile = profiles.get(camera) if profiles else {}
            if profile.get("mode") == "intercom" or profile.get("role") == "intercom":
                rows.append(profile)
        return {
            "cameras": rows,
            "policy": "Face recognition alone never authorizes an unlock.",
            "recommended": {
                "reference_photos": "5-10 diverse clear photos",
                "face_size": "Use the visual test; 120px or more is a strong starting point",
                "second_factor": True,
            },
        }

    @app.get("/api/liveness")
    def liveness_overview():
        service = getattr(processor, "liveness", None)
        profiles = getattr(processor, "camera_profiles", None)
        blocked = []
        if processor.audit:
            blocked.extend(processor.audit.search_events(
                status="spoof_suspected", limit=50,
            )["events"])
            blocked.extend(processor.audit.search_events(
                status="liveness_unconfirmed", limit=50,
            )["events"])
            blocked.sort(
                key=lambda row: row.get("start_ts") or row.get("updated_ts") or 0,
                reverse=True,
            )
            blocked = blocked[:50]
        return {
            "status": service.status() if service else {
                "enabled": False, "model_available": False,
            },
            "cameras": [profiles.get(camera) for camera in _camera_names()] if profiles else [],
            "blocked": blocked,
            "policy": "Required liveness blocks identity; RGB PAD is not a depth/IR guarantee.",
        }

    @app.get("/api/intercom/{camera}/capture/preview")
    def intercom_capture_preview(camera: str):
        """Return only the most recent test frame, held briefly in memory."""
        if camera not in _camera_names():
            raise HTTPException(404, "Unknown camera")
        with capture_preview_lock:
            saved = capture_previews.get(camera)
        if not saved or time.time() - saved[0] > 120:
            raise HTTPException(404, "No recent camera test image")
        return Response(
            content=saved[1], media_type="image/jpeg",
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post("/api/intercom/{camera}/capture")
    def test_intercom_capture(camera: str):
        if camera not in _camera_names():
            raise HTTPException(404, "Unknown camera")
        profile = processor.camera_profiles.get(camera)
        left, top, right, bottom = profile.get("roi", [0, 0, 1, 1])
        candidates, frame_count, w, h = [], 0, 0, 0
        captured_frames = {}
        liveness_history = []
        for index in range(min(int(profile.get("burst_frames", 3)), 8)):
            if index:
                time.sleep(0.08)
            content = processor.frigate.latest_frame_bytes(camera)
            image = cv2.imdecode(np.frombuffer(content or b"", np.uint8), cv2.IMREAD_COLOR)
            if image is None:
                continue
            frame_count += 1
            h, w = image.shape[:2]
            captured_frames[index] = image.copy()
            for face in engine.faces(image):
                cx = float(face.bbox[0] + face.bbox[2]) / 2 / max(w, 1)
                cy = float(face.bbox[1] + face.bbox[3]) / 2 / max(h, 1)
                if not (left <= cx <= right and top <= cy <= bottom):
                    continue
                quality = measure_face_quality(
                    image, face, min_face_px=profile["min_face_px"],
                    min_quality=processor.min_face_quality,
                )
                matches = gallery.match_candidates(face.normed_embedding, limit=2)
                best_match = matches[0] if matches else (None, None, 0.0)
                runner_score = matches[1][2] if len(matches) > 1 else 0.0
                liveness = (
                    processor.liveness.analyze(image, face)
                    if getattr(processor, "liveness", None) else
                    {"state": "unavailable", "live": None, "score": None}
                )
                liveness_history.append(liveness)
                candidates.append({
                    "frame": index,
                    "box": [round(float(value), 1) for value in face.bbox],
                    "person": best_match[1], "match_score": best_match[2],
                    "match_margin": best_match[2] - runner_score,
                    "liveness": liveness,
                    **quality.to_dict(),
                })
        if not frame_count:
            raise HTTPException(404, "Frigate did not return a high-resolution frame")
        best = max(candidates, key=lambda row: row["score"], default=None)
        if not best:
            state, message = "no_face", "לא נמצאו פנים באזור האינטרקום"
        elif not best["usable"]:
            state, message = "improve", "נמצאו פנים, אך כדאי לשפר מרחק, תאורה או חדות"
        elif best["face_px"] < 120:
            state, message = "acceptable", "הצילום מתאים, אך התקרבות נוספת תשפר אמינות"
        else:
            state, message = "excellent", "התמונה מתאימה מאוד לזיהוי באינטרקום"
        liveness_result = (
            processor.liveness.consensus(liveness_history)
            if getattr(processor, "liveness", None) else
            {"state": "unavailable", "confirmed": False}
        )
        if profile.get("liveness_mode") == "required" and not liveness_result.get("confirmed"):
            state = "spoof" if liveness_result.get("state") == "spoof" else "liveness_pending"
            message = (
                "הצילום נראה כמו תמונה או מסך; הזיהוי נחסם"
                if state == "spoof" else
                "עדיין אין מספיק הוכחות חיוּת; הזיהוי לא יאושר לכניסה"
            )
        guidance = []
        if not best:
            guidance.append("מקמו פנים מלאות בתוך התמונה והביטו למצלמה")
        else:
            if best["face_px"] < profile["min_face_px"]:
                guidance.append(
                    f"התקרבו למצלמה: נמדדו {best['face_px']}px ונדרשים לפחות "
                    f"{profile['min_face_px']}px"
                )
            if best["sharpness"] < 0.45:
                guidance.append("החזיקו את הראש יציב ונקו את עדשת המצלמה")
            if best["illumination"] < 0.45:
                guidance.append("הוסיפו אור מול הפנים, לא מאחוריהן")
            if best["contrast"] < 0.30:
                guidance.append("שפרו את התאורה כדי להפריד את הפנים מהרקע")
            if best["frontal"] < 0.65:
                guidance.append("הביטו ישר למצלמה והימנעו מהטיית הראש")
            if best["detection"] < 0.70:
                guidance.append("ודאו שהפנים גלויות ואינן מוסתרות")
        if liveness_result.get("state") == "spoof":
            guidance.insert(0, "הסירו תמונה או מסך מהמצלמה ונסו עם אדם אמיתי")
        elif liveness_result.get("confirmed") and not guidance:
            guidance.append("התמונה ברורה ובדיקת החיוּת עברה — אין צורך לשנות דבר")

        preview_token = None
        if best and best.get("frame") in captured_frames:
            preview = captured_frames[best["frame"]]
            h, w = preview.shape[:2]
            encoded_ok, encoded = cv2.imencode(
                ".jpg", preview, [cv2.IMWRITE_JPEG_QUALITY, 88],
            )
            if encoded_ok:
                with capture_preview_lock:
                    capture_previews[camera] = (time.time(), encoded.tobytes())
                    for old_camera, (saved_at, _) in list(capture_previews.items()):
                        if time.time() - saved_at > 120:
                            capture_previews.pop(old_camera, None)
                preview_token = int(time.time() * 1000)
        return {
            "camera": camera, "width": w, "height": h, "profile": profile,
            "faces": candidates, "best": best, "frames_checked": frame_count,
            "state": state, "message": message,
            "liveness": liveness_result,
            "guidance": guidance,
            "preview_url": (
                f"api/intercom/{camera}/capture/preview?_={preview_token}"
                if preview_token is not None else None
            ),
            "unlock_policy": "second_factor_required" if profile["require_second_factor"] else "face_only_blocked",
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
                "liveness": (processor.liveness.status()
                             if getattr(processor, "liveness", None) else None),
                "access": (processor.access_control.policy()
                           if getattr(processor, "access_control", None) else None),
                "schema": getattr(processor, "migration", None),
                "watchdog": (processor.runtime_health.report()
                             if getattr(processor, "runtime_health", None) else None),
                "frigate": {
                    "secure_mode": processor.frigate.secure_mode,
                    "authenticated": bool(processor.frigate.username),
                    "tls_verified": bool(processor.frigate.verify_tls),
                },
                "suggest_threshold": float(cfg["faceid"].get("suggest_threshold", 0.40))}

    return app
