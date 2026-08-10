"""Local visual-QA fixture; not part of the production image."""
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse

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
    return {"persons": 2, "queue": 4, "open_events": 0, "suggest_threshold": .4,
            "frames": {"last_backend": "ffmpeg-auto", "cache_hits": 18,
                       "requested_mode": "auto", "fallbacks": 1},
            "vision": {"enabled": False, "model": "gemma3:4b"}}


@app.get("/api/dashboard")
def dashboard():
    return {"summary": {}, "people": [], "recent": [], "events": [],
            "gallery": {"photos": 0, "recommendation": "בדיקת ממשק"}}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8766)
