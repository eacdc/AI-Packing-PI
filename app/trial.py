"""Trial recording: every book photo of a lot, lossless, with what the station decided.

Turn it on with `trial: {record: true}` in the config, or start with --record. Each lot
gets a folder under data/trials/ that scripts/replay.py can re-run with other settings
and score against a truth list (which photos were really odd books):

    trial.json        station, job, settings used, (packing) the box list
    log.jsonl         one line per book photo and per event (gap, dropped photo, label, box)
    photos/000123.png every book photo, named by the sensor count
    refs/<job>_v1/    the reference photos each check used (v2... after a re-teach)

Saving runs in its own thread; if the SD card cannot keep up, photos are skipped and
the log says so, but the station itself never waits.
"""
from __future__ import annotations

import copy
import json
import logging
import queue
import threading
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from .store import safe_name

log = logging.getLogger("trial")


def _jsonable(value):
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, np.generic):
        return value.item()
    return value


class TrialRecorder:
    def __init__(self, root: Path, station: str, info: dict, settings: dict,
                 truth_source=None):
        """truth_source(seq) -> "odd" / a job number / None: only the mock line knows this."""
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.dir = Path(root) / f"{stamp}_{safe_name(info.get('job_no', 'lot'))}_{info.get('mode', station)}"
        (self.dir / "photos").mkdir(parents=True, exist_ok=True)
        self.t0 = time.monotonic()
        self.truth_source = truth_source
        self.truth: dict[int, str] = {}
        self.ref_versions: dict[str, int] = {}
        self.counts = {"photos": 0, "unsaved": 0}
        self.meta = {"station": station, "started": datetime.now().isoformat(timespec="seconds"),
                     **_jsonable(info), "settings": _jsonable(copy.deepcopy(settings))}
        self._write_meta()
        self._log = open(self.dir / "log.jsonl", "a", encoding="utf-8")
        self._log_lock = threading.Lock()
        self._q: queue.Queue = queue.Queue(maxsize=300)
        self._closed = False
        self._thread = threading.Thread(target=self._writer, daemon=True, name="trial-writer")
        self._thread.start()
        log.info("recording trial to %s", self.dir)

    # ---- writing -------------------------------------------------------------

    def _write_meta(self) -> None:
        (self.dir / "trial.json").write_text(json.dumps(self.meta, indent=2), encoding="utf-8")

    def _writer(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                return
            path, img = item
            path.parent.mkdir(parents=True, exist_ok=True)
            cv2.imwrite(str(path), img, [cv2.IMWRITE_PNG_COMPRESSION, 1])

    def _line(self, record: dict) -> None:
        record = {"t": round(time.monotonic() - self.t0, 3), **_jsonable(record)}
        with self._log_lock:
            if not self._closed:
                self._log.write(json.dumps(record) + "\n")
                self._log.flush()

    def _save(self, path: Path, img: np.ndarray) -> bool:
        try:
            self._q.put_nowait((path, img))
            return True
        except queue.Full:
            return False

    # ---- what the stations call ------------------------------------------------

    def photo(self, seq: int, img: np.ndarray, phase: str, **info) -> None:
        """One book photo. phase: teach | check | no_box."""
        name = f"{seq:06d}.png"
        saved = self._save(self.dir / "photos" / name, img)
        self.counts["photos"] += 1
        if not saved:
            self.counts["unsaved"] += 1
        self._line({"seq": seq, "phase": phase, "file": name if saved else None, **info})
        if self.truth_source:
            truth = self.truth_source(seq)
            if truth:
                self.truth[seq] = truth

    def event(self, kind: str, **info) -> None:
        self._line({"event": kind, **info})

    def refs(self, job_no: str, images: list[np.ndarray]) -> str:
        """The reference photos now in use for job_no; returns the folder name the log points to."""
        version = self.ref_versions.get(job_no, 0) + 1
        self.ref_versions[job_no] = version
        folder = f"{safe_name(job_no)}_v{version}"
        for i, img in enumerate(images, 1):
            self._save(self.dir / "refs" / folder / f"ref_{i:02d}.png", img)
        self.event("refs", job_no=job_no, refs=folder, count=len(images))
        return folder

    def close(self, **summary) -> None:
        with self._log_lock:
            if self._closed:
                return
            self._closed = True
            self._log.close()
        self._q.put(None)
        self._thread.join(timeout=60)  # finish writing the last photos
        self.meta.update(ended=datetime.now().isoformat(timespec="seconds"), **self.counts, **_jsonable(summary))
        self._write_meta()
        if self.truth_source and not (self.dir / "truth.txt").exists():
            write_truth(self.dir / "truth.txt", self.truth, "Truth from the mock line (it knows every book).")
        log.info("trial saved: %s (%d photos, %d not saved)", self.dir, self.counts["photos"], self.counts["unsaved"])


# ---- truth lists -------------------------------------------------------------------

TRUTH_HELP = """\
# Truth list for this trial: what each book photo REALLY was. Photo numbers are the
# file names in photos/ (the sensor count), e.g. 37 = photos/000037.png.
#
# Conveyor:  odd: <numbers>      books that are NOT the lot's title (all others are)
# Packing:   <job no>: <numbers> the real title of these photos
#            odd: <numbers>      books that are not any title of their box
#            (packing photos not listed are left out of the score)
# Both:      skip: <numbers>     leave out (a hand in view, two books together...)
# Ranges are fine: 120-125. Lines starting with # are notes.
"""


def compress(numbers) -> str:
    nums = sorted(set(numbers))
    parts, i = [], 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        parts.append(str(nums[i]) if i == j else f"{nums[i]}-{nums[j]}")
        i = j + 1
    return ", ".join(parts)


def write_truth(path: Path, truth: dict[int, str], note: str = "", comments: dict[str, str] | None = None) -> None:
    groups: dict[str, list[int]] = {}
    for seq, label in truth.items():
        groups.setdefault(label, []).append(seq)
    lines = [TRUTH_HELP]
    if note:
        lines.append(f"# {note}\n")
    order = sorted(groups, key=lambda k: (k != "odd", k != "skip", k))
    for label in order:
        if comments and label in comments:
            lines.append(f"# {comments[label]}")
        lines.append(f"{label}: {compress(groups[label])}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_truth(path: Path) -> dict[int, str]:
    truth: dict[int, str] = {}
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        label, numbers = (s.strip() for s in line.rsplit(":", 1))
        for part in numbers.replace(";", ",").split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                a, b = part.split("-", 1)
                for n in range(int(a), int(b) + 1):
                    truth[n] = label
            else:
                truth[int(part)] = label
    return truth
