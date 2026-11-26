"""Restart / shut down the Pi from the touchscreen (Settings), so nobody pulls the power on a running Pi."""
from __future__ import annotations

import logging
import subprocess
import threading

from fastapi import FastAPI, HTTPException, Request

log = logging.getLogger("system")

COMMANDS = {"reboot": ["sudo", "-n", "systemctl", "reboot"],
            "poweroff": ["sudo", "-n", "systemctl", "poweroff"]}
LOCAL = {"127.0.0.1", "::1", "localhost"}


def add_system_routes(app: FastAPI, cfg, stop) -> None:
    """stop(): ends the running lot / session so its counts are saved first."""

    @app.post("/api/system/{action}")
    def system(action: str, request: Request):
        if action not in COMMANDS:
            raise HTTPException(404)
        # Only the Pi's own screen may do this, not any PC on the office network.
        if request.client is None or request.client.host not in LOCAL:
            raise HTTPException(403, "Restart and shut down work only on the station's own screen.")
        if not cfg["mock"]:
            try:  # `sudo -l <cmd>` only asks whether the command is allowed without a password
                subprocess.run(["sudo", "-n", "-l", *COMMANDS[action][2:]], check=True, timeout=5, capture_output=True)
            except Exception:
                raise HTTPException(500, "The station may not restart the Pi: run bash scripts/install.sh again.")
        stop()
        if cfg["mock"]:
            log.info("mock run: would %s the Pi now", action)
            return {"ok": True, "mock": True}
        log.warning("%s requested from the touchscreen", action)
        # Answer the screen first, then go down.
        threading.Timer(1.0, lambda: subprocess.run(COMMANDS[action], check=False)).start()
        return {"ok": True, "mock": False}
