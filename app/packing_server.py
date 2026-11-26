"""Web UI and API for the packing station touchscreen."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .camera_routes import add_camera_routes
from .system_routes import add_system_routes

log = logging.getLogger("server")
STATIC = Path(__file__).parent / "static"


def create_packing_app(cfg, station, store, erp) -> FastAPI:
    app = FastAPI(title="Book Inspector - packing")
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    app.mount("/files/alerts", StaticFiles(directory=store.dir / "alerts"), name="alerts")

    @app.get("/")
    def index():
        return FileResponse(STATIC / "packing.html", headers={"Cache-Control": "no-store"})

    @app.get("/api/state")
    def state():
        return station.snapshot()

    @app.get("/api/stream")
    async def stream(request: Request):
        async def events():
            seen, beat = -1, 0
            while not await request.is_disconnected():
                beat += 1
                if station.version != seen or beat >= 10:
                    seen, beat = station.version, 0
                    yield f"data: {json.dumps(station.snapshot())}\n\n"
                await asyncio.sleep(0.2)
        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store"})

    @app.get("/api/photo/last.jpg")
    def photo():
        data = station.jpeg("last")
        if data is None:
            raise HTTPException(404)
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.get("/api/jobs")
    def jobs(q: str = ""):
        if erp is None:
            return {"jobs": [], "error": "ERP lookup is turned off in config"}
        try:
            return {"jobs": erp.search_jobs(q), "error": None}
        except Exception as e:
            log.warning("ERP search failed: %s", e)
            return {"jobs": [], "error": f"ERP not reachable: {e}"}

    @app.get("/api/recent-jobs")
    def recent_jobs():
        return {"jobs": store.recent_jobs()}

    @app.post("/api/session/start")
    def start(body: dict = Body(...)):
        job_no = str(body.get("job_no") or "").strip()
        if not job_no:
            raise HTTPException(400, "job_no is required")
        try:
            station.start_session(job_no, str(body.get("job_name") or ""), str(body.get("client") or ""))
        except ValueError as e:
            raise HTTPException(400, str(e))
        except Exception as e:
            log.warning("loading label list failed: %s", e)
            raise HTTPException(502, f"ERP not reachable: {e}")
        return station.snapshot()

    @app.post("/api/session/stop")
    def stop():
        station.stop_session()
        return station.snapshot()

    @app.post("/api/box/open")
    def open_box(body: dict = Body(...)):
        try:
            station.open_box(int(body["barcode"]), again=True)
        except (KeyError, ValueError) as e:
            raise HTTPException(400, str(e))
        return station.snapshot()

    @app.post("/api/teach")
    def teach(body: dict = Body(...)):
        try:
            station.start_teach(str(body.get("job_no")))
        except ValueError as e:
            raise HTTPException(400, str(e))
        return station.snapshot()

    @app.post("/api/teach/cancel")
    def cancel_teach():
        station.cancel_teach()
        return station.snapshot()

    @app.post("/api/alert/ack")
    def ack():
        station.acknowledge()
        return station.snapshot()

    @app.post("/api/test-photo")
    def test_photo():
        station.test_photo()
        return station.snapshot()

    @app.get("/api/settings")
    def get_settings():
        return {"settings": cfg.tunables()}

    @app.post("/api/settings")
    def set_settings(body: dict = Body(...)):
        applied = cfg.update(body)
        if "camera" in applied:
            station.apply_camera_settings()
        return {"applied": applied, "settings": cfg.tunables()}

    add_camera_routes(app, station)
    add_system_routes(app, cfg, station.stop_session)

    @app.get("/api/boxes")
    def boxes():
        return {"boxes": store.recent_boxes()}

    return app
