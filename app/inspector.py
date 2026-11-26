"""Conveyor station: every book is compared with the lot's references; a different title raises an alert."""
from __future__ import annotations

import json
import logging
import time
from collections import deque

import numpy as np

from .matcher import PageMatcher, TeachError
from .station import PhotoStation

log = logging.getLogger("inspector")

MODES = {"page": "Without cover", "cover": "With cover"}


class Inspector(PhotoStation):
    def __init__(self, cfg, camera, store, alarm):
        self.state = "idle"          # idle | teaching | running | teach_failed
        self.message = "Tap Start lot to begin."
        self.lot: dict | None = None
        self.matcher: PageMatcher | None = None
        self.teach_images: list[np.ndarray] = []
        self.alert: dict | None = None
        self.ref_photo: np.ndarray | None = None
        self.ref_version = 0
        self._book_times: deque[float] = deque(maxlen=500)
        super().__init__(cfg, camera, store, alarm)

    # ---- photo handling --------------------------------------------------

    def on_pulse_end(self, pulse_ms: float) -> None:
        if self.state == "running" and pulse_ms > self.cfg["sensor"]["max_pulse_ms"]:
            self.record_event("gap", seq=self._seq, pulse_ms=round(pulse_ms, 1))
            self._raise_alert("gap", "No gap between books",
                              f"Sensor stayed on {pulse_ms:.0f} ms: books touching or a jam. Count may be off.")

    def handle_photo(self, photo: np.ndarray, trig: dict) -> None:
        if self.state == "teaching":
            self.record(trig, photo, "teach")
            self._teach_step(photo)
        elif self.state == "running":
            self._check(photo, trig)

    def photo_for(self, which: str) -> np.ndarray | None:
        return self.latest_photo if which == "last" else self.ref_photo

    def preview_blocked(self) -> str | None:
        # The preview takes camera frames and CPU, so it never runs beside a lot.
        return "Stop the lot to use the live preview." if self.lot else None

    # ---- teaching and checking -------------------------------------------

    def _teach_step(self, photo: np.ndarray) -> None:
        need = self.cfg["matching"]["reference_count"]
        with self.lock:
            if self.lot is None:
                return
            self.teach_images.append(photo)
            have = len(self.teach_images)
            self.message = f"Learning this title: {have} of {need} books. Run only good books of this job."
            if have == 1:
                self._set_ref_photo(photo)
            if have < need:
                return
            images = list(self.teach_images)
        try:
            report = self._build_matcher(images)
        except TeachError as e:
            with self.lock:
                self.state, self.message = "teach_failed", str(e)
                self.teach_images = []
            self.store.add_event(self.lot["id"], "teach_failed", str(e))
            self.record_event("teach_failed", message=str(e))
            return
        self.store.save_refs(self.lot["job_no"], self.lot["mode"], images)
        if self.trial:
            self.trial.refs(self.lot["job_no"], images)
        with self.lock:
            self.lot["ok"] += len(images)
            self.state = "running"
            self.message = "Checking every book."
            self.teach_images = []
        self.store.add_event(self.lot["id"], "info", "Taught", detail=json.dumps(report))
        self.store.update_counts(self.lot["id"], self.lot["ok"], self.lot["alerts"])

    def _build_matcher(self, images: list[np.ndarray]) -> dict:
        matcher = PageMatcher(self.cfg["matching"])
        report = matcher.build(images)
        with self.lock:
            self.matcher = matcher
        log.info("matcher ready: %s", report)
        return report

    def _check(self, photo: np.ndarray, trig: dict) -> None:
        matcher, lot = self.matcher, self.lot
        if matcher is None or lot is None:
            return  # lot stopped while this photo was queued
        result = matcher.check(photo)
        self.record(trig, photo, "check", **result.to_dict())
        with self.lock:
            self._book_times.append(time.monotonic())
            self.last = {"seq": trig["seq"], **result.to_dict()}
            if result.same:
                lot["ok"] += 1
                every = self.cfg["storage"]["save_ok_every"]
                save_sample = every and lot["ok"] % every == 0
            else:
                lot["alerts"] += 1
                save_sample = False
        if save_sample:
            self.store.save_photo("samples", photo)
        if not result.same:
            image = self.store.save_photo("alerts", photo)
            self._raise_alert("different", "Different book!", result.reason, image, result.to_dict())
        self.store.update_counts(lot["id"], lot["ok"], lot["alerts"])

    def _raise_alert(self, kind: str, title: str, message: str, image: str | None = None,
                     detail: dict | None = None) -> None:
        with self.lock:
            if self.lot is None:
                return
            unacked = (self.alert or {}).get("count", 0) + 1
            self.alert = {"kind": kind, "title": title, "message": message, "image": image,
                          "time": time.strftime("%H:%M:%S"), "count": unacked}
            lot_id = self.lot["id"]
        self.store.add_event(lot_id, kind, message, image, json.dumps(detail) if detail else None)
        self.alarm.trigger()
        self._changed()

    def _set_ref_photo(self, photo: np.ndarray) -> None:
        self.ref_photo = photo
        self.ref_version += 1

    # ---- operator actions ------------------------------------------------

    def start_lot(self, job_no: str, job_name: str, client: str, mode: str, reteach: bool = False) -> None:
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode}")
        if self.lot:
            self.stop_lot()
        lot_id = self.store.start_lot(job_no, job_name, client, mode)
        refs = [] if reteach else self.store.load_refs(job_no, mode)
        with self.lock:
            self.lot = {"id": lot_id, "job_no": job_no, "job_name": job_name, "client": client,
                        "mode": mode, "ok": 0, "alerts": 0, "started": time.strftime("%H:%M")}
            self.alert, self.last = None, None
            self._book_times.clear()
            self.ref_photo = None
            self.ref_version += 1
        self.start_trial({"job_no": job_no, "job_name": job_name, "client": client, "mode": mode, "lot_id": lot_id})
        if refs:
            try:
                self._build_matcher(refs)
                if self.trial:
                    self.trial.refs(job_no, refs)
                with self.lock:
                    self._set_ref_photo(refs[0])
                    self.state = "running"
                    self.message = f"Using {len(refs)} saved reference photos. Checking every book."
                self._changed()
                return
            except TeachError:
                log.warning("saved references for %s/%s no longer agree; re-teaching", job_no, mode)
        self._begin_teach()

    def _begin_teach(self) -> None:
        with self.lock:
            self.matcher = None
            self.teach_images = []
            self.state = "teaching"
            need = self.cfg["matching"]["reference_count"]
            self.message = f"Learning this title: run {need} good books of this job."
        self._changed()

    def reteach(self) -> None:
        if self.lot:
            self.alert = None
            self._begin_teach()

    def stop_lot(self) -> None:
        with self.lock:
            lot = self.lot
            self.lot, self.matcher, self.alert = None, None, None
            self.state, self.message = "idle", "Tap Start lot to begin."
        if lot:
            self.store.update_counts(lot["id"], lot["ok"], lot["alerts"])
            self.store.end_lot(lot["id"])
            self.stop_trial(ok=lot["ok"], alerts=lot["alerts"])
        self._changed()

    def acknowledge(self) -> None:
        with self.lock:
            if self.alert and self.lot:
                self.store.add_event(self.lot["id"], "info", f"Alert acknowledged ({self.alert['kind']})")
            self.alert = None
        self._changed()

    # ---- for the screen --------------------------------------------------

    def books_per_hour(self) -> int:
        now = time.monotonic()
        with self.lock:
            recent = [t for t in self._book_times if now - t <= 60]
        if len(recent) < 2:
            return 0
        return int(round((len(recent) - 1) / max(recent[-1] - recent[0], 1e-3) * 3600))

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "station": self.cfg["station_name"],
                "state": self.state,
                "message": self.message,
                "lot": dict(self.lot) if self.lot else None,
                "mode_label": MODES.get(self.lot["mode"]) if self.lot else None,
                "teach": {"have": len(self.teach_images), "need": self.cfg["matching"]["reference_count"]},
                "alert": dict(self.alert) if self.alert else None,
                "last": dict(self.last) if self.last else None,
                "photo_seq": self.photo_seq if self.latest_photo is not None else None,
                "ref_version": self.ref_version if self.ref_photo is not None else None,
                "rate": self.books_per_hour(),
                "dropped": self.dropped,
                "recording": self.trial is not None,
                "version": self.version,
            }
