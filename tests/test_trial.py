"""Trial recording and replay, end to end. Run: python tests/test_trial.py"""
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.alarm import Alarm  # noqa: E402
from app.config import Config  # noqa: E402
from app.inspector import Inspector  # noqa: E402
from app.mock import (MOCK_LABELS, NORMAL, OTHER_BOOK, SAME_SERIES, SERIES, MockCamera, MockErp,  # noqa: E402
                      MockLine, MockPackingLine, cover, render_page)
from app.packing import PackingStation  # noqa: E402
from app.replay import Replay, Trial, with_setting, write_report  # noqa: E402
from app.store import Store  # noqa: E402
from app.trial import read_truth  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def recording_config(name: str) -> Config:
    cfg = Config(ROOT / name)
    cfg.data_dir = Path(tempfile.mkdtemp())
    cfg.values["trial"]["record"] = True
    return cfg


def trig(seq: int) -> dict:
    return {"seq": seq, "t": time.monotonic(), "end": time.monotonic()}


def only_trial(cfg: Config) -> Path:
    folders = list((cfg.data_dir / "trials").iterdir())
    assert len(folders) == 1, folders
    return folders[0]


def test_conveyor_trial():
    cfg = recording_config("config.mock.yaml")
    st = Inspector(cfg, MockCamera(MockLine(cfg["mock_settings"])), Store(cfg.data_dir), Alarm({}, True))
    odd = {20: SAME_SERIES, 40: OTHER_BOOK, 55: SAME_SERIES}
    st.trial_truth = lambda seq: "odd" if seq in odd else None
    st.start_lot("J03704_26_27", "Gatinho", "", "page")
    assert st.trial is not None
    for seq in range(1, 66):
        seed, title = odd.get(seq, NORMAL)
        st.handle_photo(render_page(seed, title, shot_seed=seq), trig(seq))
    st.stop_lot()

    folder = only_trial(cfg)
    assert len(list((folder / "photos").glob("*.png"))) == 65
    assert read_truth(folder / "truth.txt") == {20: "odd", 40: "odd", 55: "odd"}
    trial = Trial(folder)
    assert len(trial.teach) == 5 and len(trial.books) == 60

    replay = Replay(trial, workers=2)
    base = trial.meta["settings"]
    r = replay.run(base)
    assert (r["missed"], r["false_alarm"], r["caught"]) == (0, 0, 3), r
    assert r["changed"] == [], "same settings must give the live result"
    lo, hi = r["margins"]["limit_range"]
    assert lo < r["margins"]["limit"]["px"] < hi

    # A limit-only change re-uses the scores, and a far too loose limit misses the same-series books.
    loose = replay.run(with_setting(base, "matching.diff_min_patch", 1000))
    assert len(replay._scores) == 1
    assert loose["missed"] == 2 and loose["caught"] == 1, loose
    assert write_report(trial, r, base).exists()


def test_packing_trial():
    cfg = recording_config("config.mock-packing.yaml")
    store = Store(cfg.data_dir)
    for job_no, name in SERIES:
        store.save_refs(job_no, "packing", [cover(name, s) for s in range(3)])
    st = PackingStation(cfg, MockCamera(MockPackingLine(cfg["mock_settings"])), store, Alarm({}, True),
                        MockErp(per_title=2))
    books = [(j, n) for j, n in SERIES for _ in range(2)]
    books.insert(4, ("odd", None))
    truth = {i: j for i, (j, _) in enumerate(books, 1)}
    st.trial_truth = truth.get
    st.start_session(SERIES[2][0], "Gatinho", "")
    st._label_confirmed(MOCK_LABELS[0])
    for seq, (job_no, name) in enumerate(books, 1):
        img = render_page(*OTHER_BOOK, shot_seed=seq) if job_no == "odd" else cover(name, 700 + seq)
        st.handle_photo(img, trig(seq))
    assert st.box is None, "box should be complete"
    st.stop_session()

    trial = Trial(only_trial(cfg))
    assert len(trial.books) == 13 and set(trial.names) == {j for j, _ in SERIES}
    r = Replay(trial, workers=1).run(trial.meta["settings"])
    assert (r["right"], r["caught"], r["wrong_title"], r["foreign_counted"], r["rejected"]) == (12, 1, 0, 0, 0), r
    assert r["changed"] == []
    assert write_report(trial, r, trial.meta["settings"]).exists()


if __name__ == "__main__":
    t0 = time.time()
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print(f"{time.time() - t0:.1f} s")
