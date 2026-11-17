"""Matcher checks on synthetic pages. Run: python -m pytest tests  (or python tests/test_matcher.py)"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import DEFAULTS  # noqa: E402
from app.matcher import PageMatcher, TeachError  # noqa: E402
from app.mock import NORMAL, OTHER_BOOK, SAME_SERIES, render_page  # noqa: E402


def taught(refs=5):
    m = PageMatcher(DEFAULTS["matching"])
    m.build([render_page(*NORMAL, shot_seed=s) for s in range(refs)])
    return m


def test_same_title_passes():
    m = taught()
    results = [m.check(render_page(*NORMAL, shot_seed=100 + s)) for s in range(30)]
    failed = [r for r in results if not r.same]
    assert not failed, failed


def test_other_book_alerts():
    m = taught()
    for s in range(10):
        assert not m.check(render_page(*OTHER_BOOK, shot_seed=200 + s)).same


def test_same_series_different_title_alerts():
    m = taught()
    for s in range(10):
        r = m.check(render_page(*SAME_SERIES, shot_seed=300 + s))
        assert not r.same, r


def test_bad_reference_rejected():
    m = PageMatcher(DEFAULTS["matching"])
    refs = [render_page(*NORMAL, shot_seed=s) for s in range(4)] + [render_page(*OTHER_BOOK, shot_seed=9)]
    try:
        m.build(refs)
    except TeachError as e:
        assert "book 5" in str(e)
    else:
        raise AssertionError("teaching with a wrong book should fail")


def test_blank_page_uses_whole_page_compare():
    m = PageMatcher(DEFAULTS["matching"])
    rng = np.random.default_rng(0)
    blank = [np.clip(np.full((800, 1280), 220.0) + rng.normal(0, 2, (800, 1280)), 0, 255).astype(np.uint8)
             for _ in range(3)]
    report = m.build(blank)
    assert report["method"] == "whole-page"
    assert m.check(blank[0]).same


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
