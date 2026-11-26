"""What every camera station shares: sensor edge -> wait -> photo -> handle_photo().

Three threads keep slow photo handling from ever making the camera miss a book:
- the sensor callback only timestamps the edge and queues it;
- the capture thread waits for the photo delay and grabs the frame;
- the process thread calls handle_photo() (comparing, counting, alerts).
"""
from __future__ import annotations

import logging
import queue
import threading
import time

import cv2
import numpy as np

log = logging.getLogger("station")


def photo_stats(photo: np.ndarray) -> dict:
    """Numbers for setting up the camera: focus (Laplacian variance), mean brightness, and the
    share of burnt-out pixels (glare from a glossy cover)."""
    return {"sharpness": round(float(cv2.Laplacian(photo, cv2.CV_64F).var()), 1),
            "brightness": round(float(photo.mean()), 1),
            "glare": round(float((photo >= 250).mean()) * 100, 2)}


class PhotoStation:
    process_queue_size = 10

    def __init__(self, cfg, camera, store, alarm):
        self.cfg = cfg
        self.camera = camera
        self.store = store
        self.alarm = alarm
        self.lock = threading.RLock()

        self.last: dict | None = None
        self.latest_photo: np.ndarray | None = None
        self.photo_seq = 0
        self.version = 0
        self.dropped = 0
        self._seq = 0
        self._current: dict | None = None
        self.trial = None             # TrialRecorder while a lot is being recorded (app/trial.py)
        self.trial_truth = None       # mock only: seq -> what the book really was

        self.capture_q: queue.Queue = queue.Queue(maxsize=20)
        self.process_q: queue.Queue = queue.Queue(maxsize=self.process_queue_size)
        threading.Thread(target=self._capture_loop, daemon=True, name="capture").start()
        threading.Thread(target=self._process_loop, daemon=True, name="process").start()

    # ---- for subclasses --------------------------------------------------

    def handle_photo(self, photo: np.ndarray, trig: dict) -> None:
        raise NotImplementedError

    def on_pulse_end(self, pulse_ms: float) -> None:
        """Called when a book has passed the sensor; pulse_ms is how long the sensor was on."""

    def photo_for(self, which: str) -> np.ndarray | None:
        return self.latest_photo if which == "last" else None

    def shrink(self, frame: np.ndarray) -> np.ndarray:
        """Reduce a frame before it waits in the queue (keeps a deep queue small in memory)."""
        return frame

    def on_dropped(self) -> None:
        """A book's photo was lost because checking fell too far behind."""

    # ---- trial recording -----------------------------------------------------

    def start_trial(self, info: dict) -> None:
        self.stop_trial()
        if not self.cfg["trial"]["record"]:
            return
        from .trial import TrialRecorder

        settings = {k: self.cfg[k] for k in ("matching", "packing", "sensor", "camera")}
        self.trial = TrialRecorder(self.cfg.data_dir / self.cfg["trial"]["folder"], self.cfg["station"],
                                   info, settings, self.trial_truth)

    def stop_trial(self, **summary) -> None:
        trial, self.trial = self.trial, None
        if trial:
            trial.close(dropped=self.dropped, **summary)

    def record(self, trig: dict, photo: np.ndarray, phase: str, **info) -> None:
        trial = self.trial
        if trial is None:
            return
        end = trig.get("end")
        trial.photo(trig["seq"], photo, phase,
                    sensor_t=round(trig["t"] - trial.t0, 3),
                    pulse_ms=round((end - trig["t"]) * 1000, 1) if end else None,
                    lag_ms=round((time.monotonic() - trig["t"]) * 1000, 1),
                    queued=self.process_q.qsize(), **info)

    def record_event(self, kind: str, **info) -> None:
        if self.trial:
            self.trial.event(kind, **info)

    def _drop(self, seq: int, where: str) -> None:
        self.dropped += 1
        self.record_event("dropped", seq=seq, where=where)
        self.on_dropped()

    # ---- sensor callbacks (keep these fast) --------------------------------

    def on_book_start(self, t: float) -> None:
        self._seq += 1
        trig = {"seq": self._seq, "t": t, "end": None}
        self._current = trig
        try:
            self.capture_q.put_nowait(trig)
        except queue.Full:
            self._drop(trig["seq"], "camera busy")

    def on_book_end(self, t: float) -> None:
        trig = self._current
        if trig is None:
            return
        trig["end"] = t
        self.on_pulse_end((t - trig["t"]) * 1000)

    # ---- worker threads --------------------------------------------------

    def _capture_loop(self) -> None:
        while True:
            trig = self.capture_q.get()
            sensor = self.cfg["sensor"]
            wait = trig["t"] + sensor["capture_delay_ms"] / 1000 - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            end = trig["end"]
            if end is not None and (end - trig["t"]) * 1000 < sensor["min_pulse_ms"]:
                self.record_event("blip", seq=trig["seq"], pulse_ms=round((end - trig["t"]) * 1000, 1))
                continue  # noise blip, not a book
            try:
                frame = self.shrink(self.camera.grab())
            except Exception:
                log.exception("camera grab failed")
                continue
            try:
                self.process_q.put((trig, frame), timeout=0.5)
            except queue.Full:
                log.warning("processing too slow, photo dropped")
                self._drop(trig["seq"], "checking behind")

    def _process_loop(self) -> None:
        while True:
            item = self.process_q.get()
            if callable(item):  # an event that must be handled in order with the photos (packing labels)
                try:
                    item()
                except Exception:
                    log.exception("queued event failed")
                self._changed()
                continue
            trig, frame = item
            try:
                photo = self.prepare_photo(frame)
                with self.lock:
                    self.latest_photo = photo
                    self.photo_seq += 1
                self.handle_photo(photo, trig)
            except Exception:
                log.exception("processing failed")
            self._changed()

    def prepare_photo(self, frame: np.ndarray) -> np.ndarray:
        cam = self.cfg["camera"]
        if cam["rotate"]:
            frame = np.rot90(frame, k=-int(cam["rotate"]) // 90)
        if cam["crop"]:
            x, y, w, h = cam["crop"]
            frame = frame[y:y + h, x:x + w]
        return np.ascontiguousarray(frame)

    def _changed(self) -> None:
        self.version += 1

    # ---- shared operator actions -----------------------------------------

    def test_photo(self) -> None:
        """Grab a photo now, without the sensor, to aim the camera and check focus/light."""
        photo = self.prepare_photo(self.camera.grab())
        with self.lock:
            self.latest_photo = photo
            self.photo_seq += 1
            self.last = {"seq": -int(time.time()), "same": None, "reason": "Test photo", **photo_stats(photo)}
        self._changed()

    def apply_camera_settings(self) -> None:
        cam = self.cfg["camera"]
        self.camera.set_exposure(cam["exposure_us"], cam["gain"])

    def try_camera(self, exposure_us: int, gain: float) -> None:
        """Exposure/gain for the Settings preview only; Save keeps them, closing reverts."""
        self.camera.set_exposure(int(exposure_us), float(gain))

    # ---- live preview in Settings -------------------------------------------

    preview_stats: dict | None = None

    def preview_blocked(self) -> str | None:
        """Why the live preview may not run now (None = it may)."""
        return None

    def preview_jpegs(self, fps: float = 5.0, seconds: float = 300.0):
        """JPEG frames for the Settings live preview; stops by itself after `seconds`."""
        period, end, n = 1.0 / fps, time.monotonic() + seconds, 0
        self.preview_stats = None
        while time.monotonic() < end and self.preview_blocked() is None:
            t = time.monotonic()
            photo = self.prepare_photo(self.camera.grab())
            ok, buf = cv2.imencode(".jpg", photo, [cv2.IMWRITE_JPEG_QUALITY, 75])
            n += 1
            self.preview_stats = {"frame": n, **photo_stats(photo)}
            if ok:
                yield buf.tobytes()
            time.sleep(max(0.0, period - (time.monotonic() - t)))

    def jpeg(self, which: str) -> bytes | None:
        with self.lock:
            img = self.photo_for(which)
        if img is None:
            return None
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
        return buf.tobytes() if ok else None
