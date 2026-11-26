"""Camera setup routes shared by both stations: live preview (MJPEG), trying exposure/gain
in Settings before Save, and the saved test photos (to download on a PC)."""
from __future__ import annotations

import html
import socket

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse

NO_STORE = {"Cache-Control": "no-store"}


def lan_address() -> str | None:
    """This Pi's address on the office network. Connecting a UDP socket sends nothing;
    it only makes the system pick the network card it would use."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return None


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

    # ---- saved test photos -----------------------------------------------------

    def saved_photos() -> list:
        folder = station.test_photo_dir
        return sorted(folder.glob("test_*.png"), reverse=True) if folder.is_dir() else []

    @app.get("/api/test-photos")
    def test_photos(request: Request):
        ip = lan_address()
        port = request.url.port or 80
        return {"folder": str(station.test_photo_dir),
                "page": f"http://{ip}:{port}/test-photos" if ip else None,
                "files": [p.name for p in saved_photos()]}

    @app.get("/api/test-photos/latest.png")
    def latest_test_photo():
        files = saved_photos()
        if not files:
            raise HTTPException(404, "No test photo saved yet.")
        return FileResponse(files[0], media_type="image/png", filename=files[0].name, headers=NO_STORE)

    @app.get("/api/test-photos/{name}")
    def test_photo_file(name: str):
        path = station.test_photo_dir / name
        if not (name.startswith("test_") and name.endswith(".png")) or path.parent != station.test_photo_dir \
                or not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, media_type="image/png", filename=name)

    @app.get("/test-photos", response_class=HTMLResponse)
    def test_photo_page():
        """A plain page for a laptop on the same network: tap a photo to download it."""
        items = "".join(
            f'<a href="/api/test-photos/{html.escape(p.name)}"><img src="/api/test-photos/{html.escape(p.name)}" '
            f'loading="lazy" alt=""><span>{html.escape(p.name)}</span></a>'
            for p in saved_photos())
        return HTMLResponse(
            "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
            "<title>Test photos</title><style>"
            "body{font:15px system-ui,sans-serif;margin:16px;background:#fff;color:#111}"
            "a{display:inline-block;margin:0 12px 16px 0;color:inherit;text-decoration:none;width:320px}"
            "img{width:320px;display:block;border:1px solid #999;background:#000}"
            "span{display:block;font-size:12px;word-break:break-all;margin-top:4px}</style>"
            "<h1>Test photos</h1><p>Newest first. Click a photo to download it at full quality (PNG).</p>"
            + (items or "<p>No test photo saved yet: Settings &rarr; Take test photo.</p>"),
            headers=NO_STORE)
