"""Fake conveyor for running and testing without Pi hardware.

Renders synthetic book pages; every Nth book is a different title, alternating
between a completely different page and the hard case: same series layout with
only the title changed.
"""
from __future__ import annotations

import threading
import time

import cv2
import numpy as np

NORMAL = (1, "MEUS AMIGOS GATINHO")
SAME_SERIES = (1, "MEUS AMIGOS ELEFANTE")
OTHER_BOOK = (7, "LIVRO SELVA")


def render_page(layout_seed: int, title: str, size=(1280, 800), shot_seed: int | None = None) -> np.ndarray:
    """A grayscale photo of a book page lying on a dark belt, slightly shifted/rotated per shot."""
    w, h = size
    rng = np.random.default_rng(layout_seed)
    pw, ph = int(w * 0.62), int(h * 0.85)
    page = np.full((ph, pw), 228, np.uint8)
    cv2.putText(page, title, (40, 90), cv2.FONT_HERSHEY_TRIPLEX, 1.4, 15, 3)
    for _ in range(7):
        centre = (int(rng.integers(60, pw - 60)), int(rng.integers(150, ph // 2)))
        cv2.circle(page, centre, int(rng.integers(15, 60)), int(rng.integers(0, 160)), -1)
    letters = list("abcdefghijklmnoprstu  ")
    for i in range(14):
        text = "".join(rng.choice(letters, size=int(rng.integers(25, 40))))
        cv2.putText(page, text, (40, ph // 2 + 30 + i * 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, 40, 1)

    srng = np.random.default_rng(shot_seed)
    frame = np.full((h, w), 40, np.uint8)
    x0 = (w - pw) // 2 + int(srng.integers(-25, 26))
    y0 = (h - ph) // 2 + int(srng.integers(-15, 16))
    frame[y0:y0 + ph, x0:x0 + pw] = page
    rot = cv2.getRotationMatrix2D((w / 2, h / 2), float(srng.uniform(-2, 2)), 1.0)
    frame = cv2.warpAffine(frame, rot, (w, h), borderValue=40)
    frame = frame.astype(np.float32) * float(srng.uniform(0.85, 1.1)) + srng.normal(0, 4, frame.shape)
    return np.clip(frame, 0, 255).astype(np.uint8)


class MockLine:
    """Fires sensor callbacks at the configured rate and tells the mock camera which book is in view."""

    def __init__(self, cfg: dict, size=(1280, 800)):
        self.period = 3600.0 / float(cfg["books_per_hour"])
        self.odd_every = int(cfg.get("odd_book_every") or 0)
        self.size = size
        self.count = 0
        self.current = NORMAL
        self._stop = threading.Event()
        self.on_start = self.on_end = None

    def start(self, on_book_start, on_book_end) -> None:
        self.on_start, self.on_end = on_book_start, on_book_end
        threading.Thread(target=self._run, daemon=True, name="mock-line").start()

    def _run(self) -> None:
        next_t = time.monotonic() + 1.0
        while not self._stop.is_set():
            time.sleep(max(0.0, next_t - time.monotonic()))
            self.count += 1
            odd_n = self.count // self.odd_every if self.odd_every and self.count % self.odd_every == 0 else 0
            self.current = (SAME_SERIES if odd_n % 2 else OTHER_BOOK) if odd_n else NORMAL
            self.on_start(time.monotonic())
            time.sleep(self.period * 0.93)  # book length vs 15 mm gap
            self.on_end(time.monotonic())
            next_t += self.period

    def frame(self) -> np.ndarray:
        seed, title = self.current
        return render_page(seed, title, self.size, shot_seed=self.count)

    def truth(self, seq: int) -> str | None:
        """For trial recordings: "odd" if book number seq was a different title."""
        return "odd" if self.odd_every and seq % self.odd_every == 0 else None

    def close(self) -> None:
        self._stop.set()


class MockCamera:
    def __init__(self, line: MockLine):
        self.line = line
        self.light = 1.0   # exposure x gain relative to 400 µs x 4, so the Settings preview reacts

    def set_exposure(self, exposure_us: int, gain: float) -> None:
        self.light = exposure_us * gain / 1600 if exposure_us else 1.0

    def grab(self) -> np.ndarray:
        frame = self.line.frame()
        return frame if self.light == 1.0 else cv2.convertScaleAbs(frame, alpha=self.light)

    def close(self) -> None:
        pass


# The real mixed box from the ERP: 6 titles of one series per box (12 each there; fewer in mock).
SERIES = [("J03697_26_27", "Caozinho"), ("J03698_26_27", "Elefante"), ("J03704_26_27", "Gatinho"),
          ("J03705_26_27", "Pintinho"), ("J03706_26_27", "Vaquinha"), ("J03707_26_27", "Zebra")]
MOCK_LABELS = list(range(1312096, 1312104))


def cover(name: str, shot_seed: int | None = None, size=(1280, 800)) -> np.ndarray:
    return render_page(1, f"MEUS AMIGOS {name.upper()}", size, shot_seed)


class MockPackingLine:
    """Pretend packer: shows a box label, then passes that box's books under the camera.

    Box 2 is packed one book short; box 3 gets a foreign book (LIVRO SELVA) in it.
    """

    def __init__(self, cfg: dict, size=(1280, 800)):
        self.per_title = int(cfg["books_per_title"])
        self.book_s = float(cfg["seconds_per_book"])
        self.size = size
        self.view = ("empty",)
        self.count = 0
        self.shown: dict[int, str] = {}
        self._stop = threading.Event()
        self.ready = lambda: True

    def start(self, on_book_start, on_book_end) -> None:
        self.on_start, self.on_end = on_book_start, on_book_end
        threading.Thread(target=self._run, daemon=True, name="mock-packing").start()

    def truth(self, seq: int) -> str | None:
        """What book number seq really was (for trial recordings): a job number or "odd"."""
        title = self.shown.get(seq)
        if title is None:
            return None
        return next((j for j, name in SERIES if title == f"MEUS AMIGOS {name.upper()}"), "odd")

    def _book(self, seed: int, title: str) -> None:
        self.count += 1
        self.shown[self.count] = title
        self.view = ("book", seed, title)
        self.on_start(time.monotonic())
        time.sleep(self.book_s * 0.4)
        self.on_end(time.monotonic())
        time.sleep(self.book_s * 0.6)
        self.view = ("empty",)

    def _run(self) -> None:
        rng = np.random.default_rng(3)
        while not self.ready():  # wait until someone starts packing on the screen
            time.sleep(0.5)
        time.sleep(3)
        for box_no, barcode in enumerate(MOCK_LABELS):
            if self._stop.is_set():
                return
            self.view = ("label", barcode)
            time.sleep(2.5)
            self.view = ("empty",)
            time.sleep(0.5)
            books = [name for _, name in SERIES for _ in range(self.per_title)]
            rng.shuffle(books)
            if box_no == 1:
                books = books[:-1]
            for i, name in enumerate(books):
                if box_no == 2 and i == 5:
                    self._book(*OTHER_BOOK)
                self._book(1, f"MEUS AMIGOS {name.upper()}")
            time.sleep(2)

    def frame(self) -> np.ndarray:
        from .barcode import render_label

        w, h = self.size
        view = self.view
        if view[0] == "book":
            return render_page(view[1], view[2], self.size, shot_seed=10_000 + self.count)
        frame = np.full((h, w), 70, np.uint8)
        if view[0] == "label":
            label = render_label(view[1])
            lh, lw = label.shape
            y, x = (h - lh) // 2, (w - lw) // 2
            frame[y:y + lh, x:x + lw] = label
        return np.clip(frame + np.random.default_rng(self.count).normal(0, 3, frame.shape), 0, 255).astype(np.uint8)

    def close(self) -> None:
        self._stop.set()


class MockErp:
    def __init__(self, per_title: int = 12):
        self.per_title = per_title

    def box_list(self, job_no: str) -> list[dict]:
        if job_no not in {j for j, _ in SERIES}:
            return []
        return [r for b in MOCK_LABELS for r in self.box_items(b)]

    def box_items(self, barcode: int) -> list[dict]:
        if barcode not in MOCK_LABELS:
            return []
        return [{"barcode": barcode, "job_no": j, "job_name": f"Meus amigos Um Livro Recortado {name}",
                 "qty": self.per_title} for j, name in SERIES]

    JOBS = [
        {"job_no": "J03704_26_27", "job_name": "Meus amigos Um Livro Recortado Gatinho",
         "client": "EDITORA BRASILEITURA LTDA", "order_qty": 5001},
        {"job_no": "J03698_26_27", "job_name": "Meus amigos Um Livro Recortado Elefante",
         "client": "EDITORA BRASILEITURA LTDA", "order_qty": 5001},
        {"job_no": "J08232/26-27", "job_name": "LivroSelva", "client": "BRASILEITURA Brazil", "order_qty": 20000},
    ]

    def search_jobs(self, text: str, limit: int = 20) -> list[dict]:
        text = text.strip().lower()
        return [j for j in self.JOBS if text in j["job_no"].lower() or text in j["job_name"].lower()][:limit]
