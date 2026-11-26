"""Camera setup routes shared by both stations: live preview (MJPEG) and trying exposure/gain
in Settings before Save."""
from __future__ import annotations

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import StreamingResponse

NO_STORE = {"Cache-Control": "no-store"}


def add_camera_routes(app: FastAPI, station) -> None:
    @app.get("/api/preview/stats")
    def preview_stats():
        return {"blocked": station.preview_blocked(), "stats": station.preview_stats}

    @app.get("/api/preview.mjpg")
    def preview():
        reason = station.preview_blocked()
        if reason:
            raise HTTPException(409, reason)

        def parts():
            for jpg in station.preview_jpegs():
                yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(jpg)) + jpg + b"\r\n"

        return StreamingResponse(parts(), media_type="multipart/x-mixed-replace; boundary=frame", headers=NO_STORE)

    @app.post("/api/camera/try")
    def camera_try(body: dict = Body(...)):
        exposure = min(max(int(body.get("exposure_us", 0)), 0), 20000)
        gain = min(max(float(body.get("gain", 1.0)), 1.0), 16.0)
        station.try_camera(exposure, gain)
        return {"exposure_us": exposure, "gain": gain}

    @app.post("/api/camera/revert")
    def camera_revert():
        station.apply_camera_settings()
        return {"ok": True}
