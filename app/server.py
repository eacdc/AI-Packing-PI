"""Web UI and API for the touchscreen (Chromium kiosk on the Pi) and for the office on the LAN."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

log = logging.getLogger("server")
STATIC = Path(__file__).parent / "static"


def create_app(cfg, inspector, store, erp) -> FastAPI:
    app = FastAPI(title="Book Inspector")
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    for sub in ("alerts", "samples"):
        app.mount(f"/files/{sub}", StaticFiles(directory=store.dir / sub), name=sub)

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/api/state")
    def state():
        return inspector.snapshot()

    @app.get("/api/stream")
    async def stream(request: Request):
        async def events():
            seen, beat = -1, 0
            while not await request.is_disconnected():
                beat += 1
                if inspector.version != seen or beat >= 10:  # resend every 2 s so the rate stays fresh
                    seen, beat = inspector.version, 0
                    yield f"data: {json.dumps(inspector.snapshot())}\n\n"
                await asyncio.sleep(0.2)
        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store"})

    @app.get("/api/photo/{which}.jpg")
    def photo(which: str):
        if which not in ("last", "ref"):
            raise HTTPException(404)
        data = inspector.jpeg(which)
        if data is None:
            raise HTTPException(404)
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.get("/api/jobs")
    def jobs(q: str = ""):
        if erp is None:
            return {"jobs": [], "error": "ERP lookup is turned off in config.yaml"}
        try:
            return {"jobs": erp.search_jobs(q), "error": None}
        except Exception as e:  # network down, login missing ...
            log.warning("ERP search failed: %s", e)
            return {"jobs": [], "error": f"ERP not reachable: {e}"}

    @app.get("/api/recent-jobs")
    def recent_jobs():
        return {"jobs": store.recent_jobs()}

    @app.post("/api/lot/start")
    def start_lot(body: dict = Body(...)):
        job_no = str(body.get("job_no") or "").strip()
        if not job_no:
            raise HTTPException(400, "job_no is required")
        try:
            inspector.start_lot(job_no, str(body.get("job_name") or ""), str(body.get("client") or ""),
                                str(body.get("mode") or "page"), bool(body.get("reteach")))
        except ValueError as e:
            raise HTTPException(400, str(e))
        return inspector.snapshot()

    @app.post("/api/lot/stop")
    def stop_lot():
        inspector.stop_lot()
        return inspector.snapshot()

    @app.post("/api/lot/reteach")
    def reteach():
        inspector.reteach()
        return inspector.snapshot()

    @app.post("/api/alert/ack")
    def ack():
        inspector.acknowledge()
        return inspector.snapshot()

    @app.post("/api/test-photo")
    def test_photo():
        inspector.test_photo()
        return inspector.snapshot()

    @app.get("/api/settings")
    def get_settings():
        return {"settings": cfg.tunables()}

    @app.post("/api/settings")
    def set_settings(body: dict = Body(...)):
        applied = cfg.update(body)
        if "camera" in applied:
            inspector.apply_camera_settings()
        return {"applied": applied, "settings": cfg.tunables()}

    # Reference photos for the packing station (it fetches the "cover" ones for each title).
    @app.get("/api/references/{job_no}/{mode}")
    def references(job_no: str, mode: str):
        if mode not in ("page", "cover"):
            raise HTTPException(404)
        return {"files": [f.name for f in sorted(store.ref_dir(job_no, mode).glob("ref_*.png"))]}

    @app.get("/api/references/{job_no}/{mode}/{name}")
    def reference_file(job_no: str, mode: str, name: str):
        path = store.ref_dir(job_no, mode) / name
        if mode not in ("page", "cover") or not name.startswith("ref_") or path.parent != store.ref_dir(job_no, mode) \
                or not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, media_type="image/png")

    @app.get("/api/lots")
    def lots():
        return {"lots": store.recent_lots()}

    @app.get("/api/lots/{lot_id}/alerts")
    def lot_alerts(lot_id: int):
        return {"alerts": store.lot_alerts(lot_id)}

    return app
