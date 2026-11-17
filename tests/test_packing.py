"""Packing station rules without a camera loop. Run: python tests/test_packing.py"""
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.alarm import Alarm  # noqa: E402
from app.config import Config  # noqa: E402
from app.mock import MOCK_LABELS, SERIES, MockCamera, MockErp, MockPackingLine, cover  # noqa: E402
from app.packing import PackingStation  # noqa: E402
from app.store import Store  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def make_station():
    cfg = Config(ROOT / "config.mock-packing.yaml")
    store = Store(Path(tempfile.mkdtemp()))
    for job_no, name in SERIES:
        store.save_refs(job_no, "packing", [cover(name, s) for s in range(3)])
    line = MockPackingLine(cfg["mock_settings"])  # never started: no fake books or labels
    station = PackingStation(cfg, MockCamera(line), store, Alarm({}, True), MockErp(per_title=2))
    station.start_session(SERIES[2][0], "Gatinho", "")
    return station


def pack(station, names):
    for i, name in enumerate(names):
        station.handle_photo(cover(name, 500 + i), {"seq": i})


def test_label_rules_and_counting():
    st = make_station()
    assert len(st.labels) == len(MOCK_LABELS)

    st._label_confirmed(9999999)
    assert st.alert["kind"] == "unknown_label" and st.box is None

    first = MOCK_LABELS[0]
    st._label_confirmed(first)
    assert st.box["barcode"] == first
    pack(st, [n for _, n in SERIES for _ in range(2)])
    assert st.box is None, "box should be complete"
    assert st.store.box_record(first)["status"] == "complete"

    st._last_closed = None   # pretend the label comes back later
    st._label_alerted.clear()
    st._label_confirmed(first)
    assert st.alert["kind"] == "packed_label" and st.box is None
    st.open_box(first, again=True)
    assert st.box["barcode"] == first and st.box["again"]


def test_over_count_and_short_box():
    st = make_station()
    st._label_confirmed(MOCK_LABELS[1])
    pack(st, ["Gatinho", "Gatinho", "Gatinho"])
    assert st.alert["kind"] == "over_count"
    st._label_confirmed(MOCK_LABELS[2])          # next box while the first is short
    assert st.alert["kind"] == "short_box"
    assert st.store.box_record(MOCK_LABELS[1])["status"] == "short"
    assert st.box["barcode"] == MOCK_LABELS[2]


if __name__ == "__main__":
    t0 = time.time()
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print(f"{time.time() - t0:.1f} s")
