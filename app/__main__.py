"""Start a station:  python -m app  [--config config.yaml] [--mock] [--port 8080]

config.yaml        -> conveyor station (station: conveyor)
config.packing.yaml -> packing station (station: packing)
"""
from __future__ import annotations

import argparse
import json
import logging
import urllib.parse
import urllib.request
from pathlib import Path

import cv2
import numpy as np
import uvicorn

from .alarm import Alarm
from .config import Config
from .store import Store

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("main")


def conveyor_ref_importer(base_url: str):
    """Fetch a title's cover reference photos from the conveyor station over the LAN."""
    def fetch(job_no: str) -> list[np.ndarray]:
        url = f"{base_url.rstrip('/')}/api/references/{urllib.parse.quote(job_no, safe='')}/cover"
        with urllib.request.urlopen(url, timeout=5) as r:
            names = json.load(r)["files"]
        images = []
        for name in names:
            with urllib.request.urlopen(f"{url}/{name}", timeout=5) as r:
                img = cv2.imdecode(np.frombuffer(r.read(), np.uint8), cv2.IMREAD_GRAYSCALE)
            if img is not None:
                images.append(img)
        return images
    return fetch


def main() -> None:
    ap = argparse.ArgumentParser(description="Book Inspector station")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--mock", action="store_true", help="run without Pi hardware (fake camera and sensor)")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--record", action="store_true",
                    help="trial: save every book photo for scripts/replay.py (data/trials/)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = Config(args.config)
    if args.record:
        cfg.values["trial"]["record"] = True
    mock = args.mock or bool(cfg["mock"])
    cfg.values["mock"] = mock   # the web routes read it (a mock run must never restart this computer)
    packing = cfg["station"] == "packing"
    cam_cfg = cfg["camera"]
    store = Store(cfg.data_dir, cfg["storage"]["jpeg_quality"])

    if mock:
        from . import mock as m

        size = (cam_cfg["width"], cam_cfg["height"])
        if packing:
            line = m.MockPackingLine(cfg["mock_settings"], size)
            erp = m.MockErp(per_title=cfg["mock_settings"]["books_per_title"])
            for job_no, name in m.SERIES:  # pretend these covers were taught earlier
                if not store.load_refs(job_no, "packing"):
                    store.save_refs(job_no, "packing", [m.cover(name, s, size) for s in range(3)])
        else:
            line = m.MockLine(cfg["mock_settings"], size)
            erp = m.MockErp()
        camera, trigger = m.MockCamera(line), line
    else:
        from .camera import open_camera
        from .erp import ErpClient
        from .trigger import SensorTrigger

        camera, trigger = open_camera(cam_cfg), SensorTrigger(cfg["sensor"])
        erp = ErpClient(cfg["erp"]) if cfg["erp"]["enabled"] else None

    alarm = Alarm(cfg["alarm"], mock)
    if packing:
        from .packing import PackingStation
        from .packing_server import create_packing_app

        url = cfg["packing"]["conveyor_url"]
        station = PackingStation(cfg, camera, store, alarm, erp, conveyor_ref_importer(url) if url else None)
        if mock:
            trigger.ready = lambda: station.session is not None
        app = create_packing_app(cfg, station, store, erp)
        stop = station.stop_session
    else:
        from .inspector import Inspector
        from .server import create_app

        station = Inspector(cfg, camera, store, alarm)
        app = create_app(cfg, station, store, erp)
        stop = station.stop_lot

    if mock:
        station.trial_truth = line.truth  # a mock trial writes its own truth.txt
    trigger.start(station.on_book_start, station.on_book_end)
    log.info("%s station ready on http://%s:%d (mock=%s)", cfg["station"], args.host, args.port, mock)
    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    finally:
        stop()
        trigger.close()
        camera.close()


if __name__ == "__main__":
    main()
