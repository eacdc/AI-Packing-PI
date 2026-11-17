"""Local storage: SQLite log of lots and alerts, alert photos, and reference photos per job."""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

SCHEMA = """
CREATE TABLE IF NOT EXISTS lots (
    id INTEGER PRIMARY KEY,
    job_no TEXT, job_name TEXT, client TEXT, mode TEXT,
    started TEXT, ended TEXT,
    ok INTEGER DEFAULT 0, alerts INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    lot_id INTEGER, ts TEXT, kind TEXT, message TEXT, image TEXT, detail TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_lot ON events(lot_id);
CREATE TABLE IF NOT EXISTS boxes (
    barcode INTEGER PRIMARY KEY,
    lot_id INTEGER, started TEXT, ended TEXT,
    status TEXT,        -- complete | short
    items TEXT          -- JSON: [{job_no, job_name, qty, counted}]
);
"""


def safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9-]+", "_", text).strip("_") or "job"


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Store:
    def __init__(self, data_dir: Path, jpeg_quality: int = 85):
        self.dir = Path(data_dir)
        for sub in ("alerts", "samples", "references"):
            (self.dir / sub).mkdir(parents=True, exist_ok=True)
        self.jpeg_quality = jpeg_quality
        self.lock = threading.Lock()
        self.db = sqlite3.connect(self.dir / "inspector.db", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        with self.db:  # lots left open by a power cut or crash end at restart
            self.db.execute("UPDATE lots SET ended = ? WHERE ended IS NULL", (now(),))

    # ---- lots and events ---------------------------------------------------

    def start_lot(self, job_no: str, job_name: str, client: str, mode: str) -> int:
        with self.lock, self.db:
            cur = self.db.execute(
                "INSERT INTO lots (job_no, job_name, client, mode, started) VALUES (?, ?, ?, ?, ?)",
                (job_no, job_name, client, mode, now()))
            return int(cur.lastrowid)

    def end_lot(self, lot_id: int) -> None:
        with self.lock, self.db:
            self.db.execute("UPDATE lots SET ended = ? WHERE id = ?", (now(), lot_id))

    def update_counts(self, lot_id: int, ok: int, alerts: int) -> None:
        with self.lock, self.db:
            self.db.execute("UPDATE lots SET ok = ?, alerts = ? WHERE id = ?", (ok, alerts, lot_id))

    def add_event(self, lot_id: int | None, kind: str, message: str, image: str | None = None,
                  detail: str | None = None) -> None:
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO events (lot_id, ts, kind, message, image, detail) VALUES (?, ?, ?, ?, ?, ?)",
                (lot_id, now(), kind, message, image, detail))

    def recent_lots(self, limit: int = 30) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM lots ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def lot_alerts(self, lot_id: int) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT ts, kind, message, image FROM events WHERE lot_id = ? AND kind != 'info' ORDER BY id DESC",
                (lot_id,)).fetchall()
        return [dict(r) for r in rows]

    def recent_jobs(self, limit: int = 8) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT job_no, job_name, client, MAX(started) AS last_used FROM lots "
                "GROUP BY job_no ORDER BY last_used DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ---- packed boxes (packing station) -----------------------------------------

    def save_box(self, barcode: int, lot_id: int, status: str, started: str, items: list[dict]) -> None:
        with self.lock, self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO boxes (barcode, lot_id, started, ended, status, items) VALUES (?, ?, ?, ?, ?, ?)",
                (barcode, lot_id, started, now(), status, json.dumps(items)))

    def box_record(self, barcode: int) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM boxes WHERE barcode = ?", (barcode,)).fetchone()
        return dict(row) if row else None

    def box_counts(self, lot_id: int) -> dict:
        with self.lock:
            rows = self.db.execute("SELECT status, COUNT(*) FROM boxes WHERE lot_id = ? GROUP BY status",
                                   (lot_id,)).fetchall()
        return {status: n for status, n in rows}

    def recent_boxes(self, limit: int = 50) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM boxes ORDER BY ended DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r, items=json.loads(r["items"])) for r in rows]

    # ---- photos --------------------------------------------------------------

    def save_photo(self, folder: str, gray: np.ndarray) -> str:
        """Save a JPEG under data/<folder>/<yyyymmdd>/ and return its path relative to data/."""
        stamp = datetime.now()
        rel = Path(folder) / stamp.strftime("%Y%m%d") / (stamp.strftime("%H%M%S_%f") + ".jpg")
        (self.dir / rel).parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(self.dir / rel), gray, [cv2.IMWRITE_JPEG_QUALITY, self.jpeg_quality])
        return rel.as_posix()

    def ref_dir(self, job_no: str, mode: str) -> Path:
        return self.dir / "references" / safe_name(job_no) / mode

    def save_refs(self, job_no: str, mode: str, images: list[np.ndarray]) -> None:
        folder = self.ref_dir(job_no, mode)
        folder.mkdir(parents=True, exist_ok=True)
        for old in folder.glob("ref_*.png"):
            old.unlink()
        for i, img in enumerate(images, 1):
            cv2.imwrite(str(folder / f"ref_{i:02d}.png"), img)

    def load_refs(self, job_no: str, mode: str) -> list[np.ndarray]:
        files = sorted(self.ref_dir(job_no, mode).glob("ref_*.png"))
        return [img for img in (cv2.imread(str(f), cv2.IMREAD_GRAYSCALE) for f in files) if img is not None]
