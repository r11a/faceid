"""Local visual-QA fixture; not part of the production image."""
from pathlib import Path

import uvicorn
from fastapi import Body, FastAPI
from fastapi.responses import FileResponse, Response

ROOT = Path(__file__).parents[1]
app = FastAPI()


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/persons")
def persons():
    return {
        "ronen": {"name": "רונן", "count": 7, "files": []},
        "moshe": {"name": "משה", "count": 3, "files": []},
    }


@app.get("/api/session")
def session():
    return {"id": "local", "name": "Home Assistant", "role": "admin", "tabs": ["*"], "enforced": False}


@app.get("/api/users")
def users():
    return {"recommended_photos": {"minimum": 5, "maximum": 10}, "users": [
        {"slug": "ronen", "name": "רונן", "count": 7, "photo": None, "favorite": True,
         "state": "ready", "state_label": "מוכן לזיהוי", "statistics": {"appearances": 142, "last_camera": "דלת כניסה", "avg_score": .78}},
        {"slug": "moshe", "name": "משה", "count": 3, "photo": None, "favorite": False,
         "state": "new", "state_label": "צריך עוד תמונות", "statistics": {"appearances": 12, "last_camera": "משרד", "avg_score": .64}},
    ]}


@app.get("/api/body")
def body():
    return {"pending": [], "approved": {"רונן": 5, "משה": 4}, "strangers": 7,
            "status": {"enabled": False, "armed": True, "model_available": True,
                       "threshold": .82, "confirmations_required": 3,
                       "classes": ["רונן", "משה", "__stranger__"], "authority": "advisory-only"}}


@app.get("/api/health")
def health():
    return {"version": "5.1.1", "persons": 2, "queue": 4, "open_events": 0, "suggest_threshold": .4,
            "engine": {"providers": ["CPUExecutionProvider"]}, "ai": {"enabled": False},
            "frames": {"last_backend": "ffmpeg-auto", "cache_hits": 18,
                       "requested_mode": "auto", "fallbacks": 1},
            "vision": {"enabled": False, "model": "gemma3:4b"}}


@app.get("/api/dashboard")
def dashboard():
    return {"summary": {}, "people": [], "recent": [], "events": [],
            "gallery": {"photos": 0, "recommendation": "בדיקת ממשק"}}


@app.get("/api/cameras/studio")
def cameras():
    samples = [{"event_id": "sample-1", "face_px": 84, "quality": .76,
                "status": "recognized", "person": "Ronen"},
               {"event_id": "sample-2", "face_px": 39, "quality": .31,
                "status": "low_quality", "person": None}]
    return {"window_days": 7, "cameras": [{"camera": "Front door", "min_face_px": 120,
        "enabled": True,
        "night_min_face_px": 130, "role": "intercom", "mode": "intercom", "burst_frames": 8,
        "high_resolution": True, "require_second_factor": True, "liveness_mode": "required", "roi": [.25, .1, .75, .9],
        "samples": samples, "impact": {"measured": 2, "accepted": 1, "rejected": 1},
        "funnel": {"events": 80, "face_detected": 61, "usable_face": 48, "recognized": 35}},
        {"camera": "Hallway", "min_face_px": 48, "night_min_face_px": 48,
         "enabled": False,
         "role": "observation", "mode": "standard", "burst_frames": 8,
         "high_resolution": False, "require_second_factor": True, "liveness_mode": "advisory", "roi": [0, 0, 1, 1],
         "samples": samples, "impact": {"measured": 2, "accepted": 1, "rejected": 1},
         "funnel": {"events": 54, "face_detected": 45, "usable_face": 39, "recognized": 31}}]}


@app.get("/api/cameras/{camera}/frame")
def camera_frame(camera: str):
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720"><defs><linearGradient id="g"><stop stop-color="#3d4144"/><stop offset="1" stop-color="#17191b"/></linearGradient></defs><rect width="1280" height="720" fill="url(#g)"/><rect x="90" y="90" width="1100" height="540" rx="25" fill="#25282b" stroke="#777"/><circle cx="640" cy="290" r="95" fill="#a6a6a2"/><path d="M440 600c30-190 370-190 400 0" fill="#777"/><text x="70" y="670" fill="#ddd" font-size="32">{camera}</text></svg>'''
    return Response(svg, media_type="image/svg+xml")


@app.get("/api/intercom")
def intercom():
    return {"cameras": [{"camera": "Front door"}], "recommended": {"face_size": "120px"}}


@app.get("/api/liveness")
def liveness():
    return {"status": {"enabled": True, "model_available": True, "threshold": .5,
                        "required_frames": 3}, "cameras": cameras()["cameras"],
            "blocked": []}


@app.post("/api/cameras/{camera}/profile")
def save_camera_profile(camera: str, profile: dict = Body(...)):
    return {"camera": camera, **profile}


@app.post("/api/cameras/{camera}/enabled")
def set_camera_enabled(camera: str, body: dict = Body(...)):
    return {"ok": True, "camera": camera, "enabled": bool(body.get("enabled"))}


@app.post("/api/intercom/{camera}/capture")
def capture_intercom(camera: str):
    return {"camera": camera, "state": "excellent", "message": "הצילום עבר את הבדיקה",
            "frames_checked": 3, "width": 1280, "height": 720,
            "best": {"face_px": 146, "score": .88, "sharpness": .82,
            "illumination": .76, "contrast": .71, "frontal": .90,
            "detection": .95, "box": [560, 180, 706, 326],
            "person": "Ronen", "match_score": .81, "match_margin": .19},
            "liveness": {"state": "live", "confirmed": True, "live_frames": 3,
                         "required_frames": 3, "score": .93},
            "profile": cameras()["cameras"][0],
            "guidance": ["התמונה ברורה ובדיקת החיוּת עברה — אין צורך לשנות דבר"],
            "preview_url": f"api/intercom/{camera}/capture/preview?_=1"}


@app.get("/api/intercom/{camera}/capture/preview")
def capture_preview(camera: str):
    return camera_frame(camera)


@app.get("/api/visits")
def visits():
    return {"camera_roles_configured": True, "visits": [{"person": "Ronen",
        "start_ts": 1786370400, "end_ts": 1786370820, "first_camera": "Front door",
        "last_camera": "Hallway", "route": ["Front door", "Hallway"], "events": ["1", "2"],
        "timeline": [{"event_id": "1", "camera": "Front door", "start_ts": 1786370400},
                     {"event_id": "2", "camera": "Hallway", "start_ts": 1786370820}],
        "event_count": 2, "avg_score": .82, "duration_seconds": 420, "open": False,
        "arrival": "confirmed", "departure": "not_observed"}]}


@app.get("/api/guests")
def guests():
    return {"threshold": .62, "margin": .12, "history": [], "guests": [{
        "id": "guest1", "name": "Delivery", "photo": "api/cameras/Front%20door/frame",
        "valid_from": 1786370400, "valid_until": 1786456800, "max_entries": 1,
        "entries_used": 0, "allowed_cameras": ["Front door"], "status": "active",
    }]}


@app.get("/api/site-map")
def site_map(days: int = 7):
    return {"map": {"title": "Office", "notice": "Estimated locations, not GPS.",
        "links": [["Front door", "Hallway"]], "cameras": [
            {"camera": "Front door", "label": "Entrance", "x": 24, "y": 45,
             "role": "intercom", "enabled": True},
            {"camera": "Hallway", "label": "Hallway", "x": 72, "y": 45,
             "role": "observation", "enabled": False}]},
        "people": [{"person": "Ronen", "camera": "Hallway", "last_seen": 1786370820,
                    "open": False, "route": ["Front door", "Hallway"]}],
        "analytics": {"days": days, "total_person_events": 134, "peak_hour": 18,
            "cameras": [{"camera": "Front door", "events": 80, "share": .597},
                        {"camera": "Hallway", "events": 54, "share": .403}],
            "hours": [], "transitions": [{"from": "Front door", "to": "Hallway", "count": 27}]}}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8766)
