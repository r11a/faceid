"""Local visual-QA fixture; not part of the production image."""
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse, Response

ROOT = Path(__file__).parents[1]
app = FastAPI()


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/persons")
def persons():
    return {"ronen": {"name": "רונן", "files": []}, "moshe": {"name": "משה", "files": []}}


@app.get("/api/body")
def body():
    return {"pending": [], "approved": {"רונן": 5, "משה": 4}, "strangers": 7,
            "status": {"enabled": False, "armed": True, "model_available": True,
                       "threshold": .82, "confirmations_required": 3,
                       "classes": ["רונן", "משה", "__stranger__"], "authority": "advisory-only"}}


@app.get("/api/health")
def health():
    return {"version": "3.1.3", "persons": 2, "queue": 4, "open_events": 0, "suggest_threshold": .4,
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
    return {"window_days": 7, "cameras": [{"camera": "Front door", "min_face_px": 56,
        "role": "entry", "samples": samples, "impact": {"measured": 2, "accepted": 1, "rejected": 1},
        "funnel": {"events": 80, "face_detected": 61, "usable_face": 48, "recognized": 35}},
        {"camera": "Hallway", "min_face_px": 48, "role": "observation", "samples": samples,
         "impact": {"measured": 2, "accepted": 1, "rejected": 1},
         "funnel": {"events": 54, "face_detected": 45, "usable_face": 39, "recognized": 31}}]}


@app.get("/api/cameras/{camera}/frame")
def camera_frame(camera: str):
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720"><defs><linearGradient id="g"><stop stop-color="#3d4144"/><stop offset="1" stop-color="#17191b"/></linearGradient></defs><rect width="1280" height="720" fill="url(#g)"/><rect x="90" y="90" width="1100" height="540" rx="25" fill="#25282b" stroke="#777"/><circle cx="640" cy="290" r="95" fill="#a6a6a2"/><path d="M440 600c30-190 370-190 400 0" fill="#777"/><text x="70" y="670" fill="#ddd" font-size="32">{camera}</text></svg>'''
    return Response(svg, media_type="image/svg+xml")


@app.get("/api/visits")
def visits():
    return {"camera_roles_configured": True, "visits": [{"person": "Ronen",
        "start_ts": 1786370400, "end_ts": 1786370820, "first_camera": "Front door",
        "last_camera": "Hallway", "route": ["Front door", "Hallway"], "events": ["1", "2"],
        "event_count": 2, "avg_score": .82, "duration_seconds": 420, "open": False,
        "arrival": "confirmed", "departure": "not_observed"}]}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8766)
