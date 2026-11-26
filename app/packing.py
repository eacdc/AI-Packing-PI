"""Packing station: the box label must be in the job's label list, and the books put in the box
must be the titles and quantities that label carries in the ERP.

Flow:
1. Start packing: pick the job. Its list of packing-slip labels comes from the ERP
   (every box that has this job, with every title in that box and the qty per box).
2. Show a box label to the camera. It must be in the list and not packed before.
3. Pass each book under the camera (the sensor takes the photo). Each photo is matched
   against the reference photos of this box's titles only, and counted.
4. When every title reaches its qty the box is complete. Showing the next label while
   the box is short logs it as short.
"""
from __future__ import annotations

import json
import logging
import threading
import time

import cv2
import numpy as np

from .barcode import read_label_numbers
from .matcher import PageMatcher, TeachError
from .station import PhotoStation

log = logging.getLogger("packing")

REF_MODE = "packing"  # reference photos taken by the packing camera


def score_titles(photo: np.ndarray, matchers: dict[str, PageMatcher]) -> dict[str, tuple[int, int]]:
    """job_no -> (print points that line up, largest changed patch) for one book photo."""
    if not matchers:
        return {}
    page = next(iter(matchers.values())).prepare(photo)
    return {j: m.score_page(page) for j, m in matchers.items()}


def pick_title(full: dict[str, tuple[int, int]], matchers: dict[str, PageMatcher], names: dict[str, str],
               cfg: dict, job_nos: list[str]) -> tuple[str | None, dict]:
    """The box title the scores point to, or None with the reason (shared with scripts/replay.py)."""
    missing = [names.get(j, j) for j in job_nos if j not in matchers]
    detail = {"reason": "", "scores": {j: {"points": p, "patch": d} for j, (p, d) in full.items()}, "ms": 0.0}
    if not matchers:
        detail["reason"] = "No reference photos for this box's titles. Teach them first."
        return None, detail
    # Titles whose print does not line up at all are out; among the rest the smallest patch wins.
    aligned = [j for j in full if full[j][0] >= matchers[j].inlier_threshold]
    if not aligned:
        detail["reason"] = "Book not recognised: not one of this box's titles" + (
            f" (no references yet for {', '.join(missing)})" if missing else "")
        return None, detail
    ranked = sorted(aligned, key=lambda j: (full[j][1], -full[j][0]))
    best = ranked[0]
    patch = full[best][1]
    detail["title"] = names.get(best, best)
    detail["best"] = best
    if patch > matchers[best].diff_threshold * cfg["patch_factor"]:
        detail["reason"] = (f"Looks like {detail['title']} but part of the cover differs: "
                            "a title that is not in this box?")
        return None, detail
    if len(ranked) > 1 and full[ranked[1]][1] - patch < cfg["min_patch_gap"]:
        detail["reason"] = (f"Not sure: {detail['title']} or {names.get(ranked[1], ranked[1])}. "
                            "Show the book again.")
        return None, detail
    detail["reason"] = "ok"
    return best, detail


class PackingStation(PhotoStation):
    # Hand packing comes in bursts; a deep queue lets checking catch up instead of losing a
    # book (a lost photo is an uncounted book). Photos are shrunk first to keep it small.
    process_queue_size = 60

    def __init__(self, cfg, camera, store, alarm, erp, ref_import=None):
        self.erp = erp
        self.ref_import = ref_import    # callable(job_no) -> list of images from the conveyor station
        self.session: dict | None = None
        self.labels: dict[int, list[dict]] = {}     # barcode -> [{job_no, job_name, qty}]
        self.titles: dict[str, dict] = {}           # job_no -> {"job_name", "matcher", "refs"}
        self.box: dict | None = None
        self.teach: dict | None = None              # {"job_no", "images"}
        self.alert: dict | None = None
        self.message = "Tap Start packing and pick the job."
        self._label_seen: dict[int, tuple[int, float]] = {}   # barcode -> (reads, first read time)
        self._label_alerted: dict[int, float] = {}
        self._last_closed: int | None = None
        super().__init__(cfg, camera, store, alarm)
        threading.Thread(target=self._label_loop, daemon=True, name="labels").start()

    # ---- session -----------------------------------------------------------

    def start_session(self, job_no: str, job_name: str, client: str) -> None:
        rows = self.erp.box_list(job_no) if self.erp else []
        if not rows:
            raise ValueError(f"No packing-slip labels found in the ERP for {job_no}.")
        if self.session:
            self.stop_session()
        labels: dict[int, list[dict]] = {}
        for r in rows:
            labels.setdefault(int(r["barcode"]), []).append(
                {"job_no": r["job_no"], "job_name": r["job_name"], "qty": int(r["qty"])})
        # Load every title's references before the session goes live, so no book is
        # checked against a half-loaded set of titles.
        titles: dict[str, dict] = {}
        for items in labels.values():
            for item in items:
                if item["job_no"] not in titles:
                    titles[item["job_no"]] = self._load_title(item["job_no"], item["job_name"])
        lot_id = self.store.start_lot(job_no, job_name, client, "packing")
        self.start_trial({"job_no": job_no, "job_name": job_name, "client": client, "mode": "packing",
                          "lot_id": lot_id, "labels": labels,
                          "titles": {j: t["job_name"] for j, t in titles.items()}})
        ref_images = {j: t.pop("images") for j, t in titles.items() if "images" in t}
        if self.trial:
            for j, images in ref_images.items():
                self.trial.refs(j, images)
        with self.lock:
            self.session = {"id": lot_id, "job_no": job_no, "job_name": job_name, "client": client,
                            "started": time.strftime("%H:%M")}
            self.labels, self.titles = labels, titles
            self.box, self.alert, self.teach, self.last = None, None, None, None
            self.message = f"{len(labels)} box labels for this job. Show a box label to the camera."
        self._changed()

    def stop_session(self) -> None:
        self._close_box()
        with self.lock:
            session = self.session
            self.session, self.box, self.teach, self.alert = None, None, None, None
            self.labels, self.titles = {}, {}
            self.message = "Tap Start packing and pick the job."
        if session:
            self.store.end_lot(session["id"])
            self.stop_trial(boxes=self.store.box_counts(session["id"]))
        self._changed()

    def _load_title(self, job_no: str, job_name: str) -> dict:
        title = {"job_name": job_name, "matcher": None, "refs": 0, "source": None}
        images = self.store.load_refs(job_no, REF_MODE)
        source = "packing camera"
        if not images and self.ref_import:
            try:
                images = self.ref_import(job_no)
                source = "conveyor station"
            except Exception as e:
                log.warning("could not import references for %s: %s", job_no, e)
        if not images:
            return title
        try:
            matcher = PageMatcher(self.cfg["matching"])
            matcher.build(images)
        except TeachError as e:
            log.warning("references for %s (%s) rejected: %s", job_no, source, e)
            return title
        return dict(title, matcher=matcher, refs=len(images), source=source, images=images)

    # ---- labels --------------------------------------------------------------

    def _label_loop(self) -> None:
        interval = self.cfg["packing"]["label_scan_ms"] / 1000
        while True:
            time.sleep(interval)
            if self.session is None or not self.capture_q.empty():
                continue  # a book photo is about to be taken
            try:
                numbers = read_label_numbers(self.camera.grab())  # whole view: the label may be outside the book crop
            except Exception:
                log.exception("label scan failed")
                continue
            now = time.monotonic()
            for n in set(numbers):
                count, first = self._label_seen.get(n, (0, now))
                if now - first > 3:
                    count, first = 0, now
                self._label_seen[n] = (count + 1, first)
                if count + 1 == self.cfg["packing"]["label_confirm_reads"]:
                    # Handled in the photo queue, so books photographed before the label
                    # still count into the box that was open when they were photographed.
                    self.process_q.put(lambda b=n: self._label_confirmed(b))

    def _label_confirmed(self, barcode: int) -> None:
        if self.session is None or (self.box and self.box["barcode"] == barcode) or barcode == self._last_closed:
            return  # the open box, or the box just finished still in view
        if barcode not in self.labels:
            if self._lookup_late_label(barcode):
                pass  # printed after the list was loaded; now in self.labels
            else:
                self._alert_once(barcode, "unknown_label", "Wrong box label",
                                 f"Label {barcode:07d} is not in the list for {self.session['job_no']}.")
                return
        record = self.store.box_record(barcode)
        if record:
            self._alert_once(barcode, "packed_label", "Box already done",
                             f"Box {barcode:07d} was {record['status']} at {record['ended'][11:16]}. "
                             "Tap Open box again to repack it.")
            return
        self.open_box(barcode)

    def _lookup_late_label(self, barcode: int) -> bool:
        """A label printed after the list was loaded: ask the ERP once."""
        if not self.erp or barcode in self._label_alerted:
            return False
        try:
            rows = self.erp.box_items(barcode)
        except Exception as e:
            log.warning("ERP lookup of %s failed: %s", barcode, e)
            return False
        if not any(r["job_no"] == self.session["job_no"] for r in rows):
            return False
        with self.lock:
            self.labels[barcode] = [{"job_no": r["job_no"], "job_name": r["job_name"], "qty": int(r["qty"])}
                                    for r in rows]
        return True

    def _alert_once(self, barcode: int, kind: str, title: str, message: str) -> None:
        """Label alerts: a label held in view is read again and again; alert once per 15 s."""
        now = time.monotonic()
        if now - self._label_alerted.get(barcode, 0) < 15:
            return
        self._label_alerted[barcode] = now
        self.record_event("label", barcode=barcode, outcome=kind)
        self._raise_alert(kind, title, message, barcode=barcode)

    def open_box(self, barcode: int, again: bool = False) -> None:
        if barcode not in self.labels:
            raise ValueError("label not in this job's list")
        self._close_box()
        items = [dict(item, counted=0) for item in self.labels[barcode]]
        with self.lock:
            self.box = {"barcode": barcode, "items": items, "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        "again": again}
            if self.alert and self.alert["kind"] in ("packed_label", "unknown_label", "no_box"):
                self.alert = None
            total = sum(i["qty"] for i in items)
            self.message = f"Box {barcode:07d}: {len(items)} titles, {total} books."
        self.store.add_event(self.session["id"], "info", f"Box {barcode:07d} opened" + (" again" if again else ""))
        self.record_event("box_open", barcode=barcode, again=again, items=self.labels[barcode])
        self._changed()

    def _close_box(self) -> None:
        with self.lock:
            box = self.box
            self.box = None
        if not box or self.session is None:
            return
        done = sum(i["counted"] for i in box["items"])
        if done == 0 and not box["again"]:
            return  # opened but nothing packed: leave it free
        complete = all(i["counted"] == i["qty"] for i in box["items"])
        status = "complete" if complete else "short"
        self.store.save_box(box["barcode"], self.session["id"], status, box["started"], box["items"])
        self.record_event("box_closed", barcode=box["barcode"], status=status, items=box["items"])
        self._last_closed = box["barcode"]
        if not complete:
            missing = ", ".join(f"{i['job_name']} {i['qty'] - i['counted']}" for i in box["items"]
                                if i["counted"] < i["qty"])
            self._raise_alert("short_box", "Previous box short",
                              f"Box {box['barcode']:07d} closed short: {missing}")

    # ---- books -----------------------------------------------------------------

    def shrink(self, frame: np.ndarray) -> np.ndarray:
        width = self.cfg["packing"]["photo_width"]
        if frame.shape[1] <= width:
            return frame
        return cv2.resize(frame, (width, round(frame.shape[0] * width / frame.shape[1])), interpolation=cv2.INTER_AREA)

    def on_dropped(self) -> None:
        self._raise_alert("missed_photo", "Book photo missed",
                          "Checking fell too far behind and one book was not photographed. Recount this box.")

    def handle_photo(self, photo: np.ndarray, trig: dict) -> None:
        if self.session is None:
            return
        if self.teach:
            self.record(trig, photo, "teach", job_no=self.teach["job_no"])
            self._teach_step(photo)
            return
        if self.box is None:
            self.record(trig, photo, "no_box")
            image = self.store.save_photo("alerts", photo)
            self._raise_alert("no_box", "No box open", "Show the box label to the camera before packing books.",
                              image=image)
            return
        job_nos = [i["job_no"] for i in self.box["items"]]
        job_no, detail = self.classify(photo, job_nos)
        self.record(trig, photo, "check", box=self.box["barcode"], titles=job_nos, result=job_no, **detail)
        with self.lock:
            self.last = {"seq": trig["seq"], **detail}
        if job_no is None:
            image = self.store.save_photo("alerts", photo)
            self._raise_alert("not_recognised", "Wrong book?", detail["reason"], image=image, detail=detail)
            return
        self._count(job_no, photo)

    def _count(self, job_no: str, photo: np.ndarray) -> None:
        with self.lock:
            box = self.box
            if box is None:
                return
            item = next(i for i in box["items"] if i["job_no"] == job_no)
            item["counted"] += 1
            over = item["counted"] > item["qty"]
            complete = all(i["counted"] == i["qty"] for i in box["items"])
            if not over:
                packed = sum(i["counted"] for i in box["items"])
                total = sum(i["qty"] for i in box["items"])
                self.message = f"Box {box['barcode']:07d}: {packed} of {total} books."
        if over:
            image = self.store.save_photo("alerts", photo)
            self._raise_alert("over_count", "Too many of one title",
                              f"{item['job_name']}: {item['counted']} counted, box needs {item['qty']}. "
                              "Take one out.", image=image)
        elif complete:
            self.store.save_box(box["barcode"], self.session["id"], "complete", box["started"], box["items"])
            self.record_event("box_closed", barcode=box["barcode"], status="complete", items=box["items"])
            self._last_closed = box["barcode"]
            with self.lock:
                self.box = None
                self.message = f"Box {box['barcode']:07d} complete. Show the next box label."
            self.store.add_event(self.session["id"], "info", f"Box {box['barcode']:07d} complete")

    def classify(self, photo: np.ndarray, job_nos: list[str]) -> tuple[str | None, dict]:
        """Which of this box's titles is the book?

        Books of one series share most of their print, so print points alone do not rank
        them reliably. Every title is scored, and the one whose cover differs least (the
        changed-patch check) wins, if it clearly beats the runner-up.
        """
        t0 = time.perf_counter()
        matchers = {j: self.titles[j]["matcher"] for j in job_nos if self.titles.get(j, {}).get("matcher")}
        names = {j: self.titles[j]["job_name"] for j in job_nos if j in self.titles}
        full = score_titles(photo, matchers)
        job_no, detail = pick_title(full, matchers, names, self.cfg["packing"], job_nos)
        detail["ms"] = round((time.perf_counter() - t0) * 1000, 1)
        return job_no, detail

    # ---- teaching ------------------------------------------------------------

    def start_teach(self, job_no: str) -> None:
        if job_no not in self.titles:
            raise ValueError("title not in this session")
        with self.lock:
            self.teach = {"job_no": job_no, "images": []}
            need = self.cfg["packing"]["reference_count"]
            self.message = f"Teaching {self.titles[job_no]['job_name']}: pass {need} books of this title."
        self._changed()

    def cancel_teach(self) -> None:
        with self.lock:
            self.teach = None
            self.message = "Teaching cancelled."
        self._changed()

    def _teach_step(self, photo: np.ndarray) -> None:
        need = self.cfg["packing"]["reference_count"]
        with self.lock:
            teach = self.teach
            teach["images"].append(photo)
            name = self.titles[teach["job_no"]]["job_name"]
            self.message = f"Teaching {name}: {len(teach['images'])} of {need} books."
            if len(teach["images"]) < need:
                return
            self.teach = None
        try:
            matcher = PageMatcher(self.cfg["matching"])
            matcher.build(teach["images"])
        except TeachError as e:
            with self.lock:
                self.message = f"Teaching {name} failed: {e}"
            self._raise_alert("teach_failed", "Teaching failed", f"{name}: {e}")
            self.record_event("teach_failed", job_no=teach["job_no"], message=str(e))
            return
        self.store.save_refs(teach["job_no"], REF_MODE, teach["images"])
        if self.trial:
            self.trial.refs(teach["job_no"], teach["images"])
        with self.lock:
            self.titles[teach["job_no"]].update(matcher=matcher, refs=len(teach["images"]), source="packing camera")
            self.message = f"{name} taught."
        # The teaching books go into the box too.
        if self.box and any(i["job_no"] == teach["job_no"] for i in self.box["items"]):
            for img in teach["images"]:
                self._count(teach["job_no"], img)

    # ---- alerts and screen ------------------------------------------------------

    def _raise_alert(self, kind: str, title: str, message: str, image: str | None = None,
                     detail: dict | None = None, barcode: int | None = None) -> None:
        with self.lock:
            if self.session is None:
                return
            unacked = (self.alert or {}).get("count", 0) + 1
            self.alert = {"kind": kind, "title": title, "message": message, "image": image, "barcode": barcode,
                          "time": time.strftime("%H:%M:%S"), "count": unacked}
            lot_id = self.session["id"]
        self.store.add_event(lot_id, kind, message, image, json.dumps(detail) if detail else None)
        self.alarm.trigger()
        self._changed()

    def acknowledge(self) -> None:
        with self.lock:
            self.alert = None
        self._changed()

    def preview_blocked(self) -> str | None:
        # The label scanner and the book photos use the camera while a session is open.
        return "End the session to use the live preview." if self.session else None

    def snapshot(self) -> dict:
        with self.lock:
            box = None
            if self.box:
                box = {"barcode": self.box["barcode"], "items": [dict(i) for i in self.box["items"]],
                       "again": self.box["again"]}
                for item in box["items"]:
                    item["taught"] = self.titles.get(item["job_no"], {}).get("matcher") is not None
            done = self.store.box_counts(self.session["id"]) if self.session else {}
            return {
                "station": self.cfg["station_name"],
                "session": dict(self.session) if self.session else None,
                "labels": len(self.labels),
                "boxes_done": done,
                "box": box,
                "titles": [{"job_no": j, "job_name": t["job_name"], "taught": t["matcher"] is not None,
                            "refs": t["refs"], "source": t.get("source")} for j, t in self.titles.items()],
                "teach": ({"job_no": self.teach["job_no"], "have": len(self.teach["images"]),
                           "need": self.cfg["packing"]["reference_count"]} if self.teach else None),
                "message": self.message,
                "alert": dict(self.alert) if self.alert else None,
                "last": dict(self.last) if self.last else None,
                "photo_seq": self.photo_seq if self.latest_photo is not None else None,
                "dropped": self.dropped,
                "recording": self.trial is not None,
                "version": self.version,
            }
