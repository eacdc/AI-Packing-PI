"""Re-run a recorded trial (app/trial.py) with any settings and score it against truth.txt.

Scoring a photo (aligning it with the references) is the slow part and runs on all CPU
cores. Settings that only move a limit (diff_factor, diff_min_patch, patch_factor,
min_patch_gap) re-use those scores, so sweeping them is instant; any other setting
scores every photo again.
"""
from __future__ import annotations

import copy
import html
import json
import os
import statistics
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import cv2

from .matcher import PageMatcher, TeachError
from .packing import pick_title, score_titles
from .trial import compress, read_truth, write_truth

# Settings that only change a limit, not the scores themselves.
DECISION_KEYS = {("matching", "diff_factor"), ("matching", "diff_min_patch"),
                 ("packing", "patch_factor"), ("packing", "min_patch_gap")}


# ---- loading a trial ------------------------------------------------------------------

class Trial:
    def __init__(self, folder: Path):
        self.dir = Path(folder)
        self.meta = json.loads((self.dir / "trial.json").read_text(encoding="utf-8"))
        self.station = self.meta["station"]
        self.books: list[dict] = []     # check photos, each with the refs in use at the time
        self.teach: list[dict] = []
        self.other: list[dict] = []     # photos taken with no box open (packing)
        self.events: list[dict] = []
        current: dict[str, str] = {}
        for line in (self.dir / "log.jsonl").read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            if "event" in rec:
                self.events.append(rec)
                if rec["event"] == "refs":
                    current[rec["job_no"]] = rec["refs"]
                continue
            rec["refs"] = (current.get(self.meta["job_no"]) if self.station == "conveyor" else dict(current))
            {"check": self.books, "teach": self.teach}.get(rec["phase"], self.other).append(rec)
        truth_file = self.dir / "truth.txt"
        self.truth = read_truth(truth_file) if truth_file.exists() else None
        self.names = self.meta.get("titles") or {self.meta["job_no"]: self.meta.get("job_name", "")}

    def count(self, kind: str) -> int:
        return sum(1 for e in self.events if e["event"] == kind)


def load_refs(folder: Path, name: str) -> list:
    files = sorted((folder / "refs" / name).glob("ref_*.png"))
    return [img for img in (cv2.imread(str(f), cv2.IMREAD_GRAYSCALE) for f in files) if img is not None]


class Matchers:
    """PageMatchers per refs folder, built once per settings."""

    def __init__(self, folder: Path, matching: dict):
        self.folder, self.matching, self.cache = Path(folder), matching, {}

    def get(self, name: str | None) -> PageMatcher | None:
        if name is None:
            return None
        if name not in self.cache:
            m = PageMatcher(self.matching)
            try:
                m.build(load_refs(self.folder, name))
            except TeachError:
                m = None
            self.cache[name] = m
        return self.cache[name]

    def for_book(self, rec: dict) -> dict[str, PageMatcher]:
        ms = {j: self.get(rec["refs"].get(j)) for j in rec["titles"]}
        return {j: m for j, m in ms.items() if m is not None}


# ---- scoring (in worker processes) --------------------------------------------------------

_W: dict = {}


def _init_worker(folder: str, station: str, matching: dict) -> None:
    cv2.setNumThreads(1)
    _W.update(folder=Path(folder), station=station, matchers=Matchers(Path(folder), matching))


def _score(rec: dict):
    img = cv2.imread(str(_W["folder"] / "photos" / rec["file"]), cv2.IMREAD_GRAYSCALE)
    if img is None:
        return None
    if _W["station"] == "conveyor":
        m = _W["matchers"].get(rec["refs"])
        if m is None:
            return None
        if m.use_ncc:
            page = m.prepare(img)
            return {"ncc": max(m._ncc(page, r) for r in m.refs)}
        n, patch = m.score_page(m.prepare(img))
        return {"n": n, "patch": patch}
    return {"full": score_titles(img, _W["matchers"].for_book(rec))}


def score_all(trial: Trial, matching: dict, workers: int) -> list:
    todo = [b for b in trial.books if b.get("file")]
    if workers <= 1:
        _init_worker(str(trial.dir), trial.station, matching)
        scores = [_score(b) for b in todo]
    else:
        with ProcessPoolExecutor(workers, initializer=_init_worker,
                                 initargs=(str(trial.dir), trial.station, matching)) as pool:
            scores = list(pool.map(_score, todo, chunksize=8))
    by_seq = {b["seq"]: s for b, s in zip(todo, scores)}
    return [by_seq.get(b["seq"]) for b in trial.books]


# ---- deciding and judging ----------------------------------------------------------------

def decide(trial: Trial, scores: list, settings: dict) -> list[dict]:
    """One verdict per book: {"seq", "flag" (conveyor: alert) / "pick" (packing), "reason", margins...}."""
    matchers = Matchers(trial.dir, settings["matching"])
    out = []
    for rec, s in zip(trial.books, scores):
        v = {"seq": rec["seq"], "live": rec}
        if s is None:
            v.update(flag=None, pick=None, reason="photo not saved or references rejected", skipped=True)
        elif trial.station == "conveyor":
            m = matchers.get(rec["refs"])
            if "ncc" in s:
                same = s["ncc"] >= m.ncc_threshold
                v.update(flag=not same, reason="ok" if same else "Page looks different",
                         ncc=round(s["ncc"], 3), patch_ratio=None, point_ratio=None)
            else:
                m.set_diff_threshold(settings["matching"])
                r = m.decide(s["n"], s["patch"])
                v.update(flag=not r.same, reason=r.reason, n=s["n"], patch=min(s["patch"], 10 ** 6),
                         n_limit=m.inlier_threshold, patch_limit=m.diff_threshold,
                         point_ratio=s["n"] / max(m.inlier_threshold, 1),
                         patch_ratio=min(s["patch"], 10 ** 6) / max(m.diff_threshold, 1))
        else:
            ms = matchers.for_book(rec)
            for m in ms.values():
                m.set_diff_threshold(settings["matching"])
            full = {j: tuple(x) for j, x in s["full"].items()}
            pick, detail = pick_title(full, ms, trial.names, settings["packing"], rec["titles"])
            ranked = sorted((full[j][1], j) for j in full if full[j][0] >= ms[j].inlier_threshold)
            v.update(pick=pick, reason=detail["reason"], best=detail.get("best"), scores=full)
            if ranked:
                best = ranked[0][1]
                v["limit_ratio"] = ranked[0][0] / max(ms[best].diff_threshold * settings["packing"]["patch_factor"], 1)
                v["gap"] = ranked[1][0] - ranked[0][0] if len(ranked) > 1 else None
        out.append(v)
    return out


def judge(trial: Trial, verdicts: list[dict]) -> dict:
    """Compare with truth.txt. Conveyor photos not listed are the lot's title; packing ones are not scored."""
    res = {"scored": 0, "mistakes": [], "unscored": 0}
    if trial.truth is None:
        res["unscored"] = len(verdicts)
        return res
    if trial.station == "conveyor":
        res.update(caught=0, missed=0, false_alarm=0, passed=0)
        for v in verdicts:
            label = trial.truth.get(v["seq"], "same")
            if label == "skip" or v.get("skipped"):
                res["unscored"] += 1
                continue
            odd = label == "odd"
            v["truth"] = "odd" if odd else "same"
            res["scored"] += 1
            key = ("caught" if v["flag"] else "missed") if odd else ("false_alarm" if v["flag"] else "passed")
            res[key] += 1
            if key in ("missed", "false_alarm"):
                v["outcome"] = key
                res["mistakes"].append(v)
    else:
        res.update(right=0, wrong_title=0, foreign_counted=0, caught=0, rejected=0)
        for v in verdicts:
            label = trial.truth.get(v["seq"])
            if label is None or label == "skip" or v.get("skipped"):
                res["unscored"] += 1
                continue
            rec = v["live"]
            odd = label == "odd" or label not in rec["titles"]
            v["truth"] = "odd" if odd else label
            res["scored"] += 1
            if odd:
                key = "foreign_counted" if v["pick"] else "caught"
            elif v["pick"] is None:
                key = "rejected"
            else:
                key = "right" if v["pick"] == label else "wrong_title"
            res[key] += 1
            if key in ("foreign_counted", "wrong_title", "rejected"):
                v["outcome"] = key
                res["mistakes"].append(v)
    return res


def margins(trial: Trial, verdicts: list[dict], settings: dict) -> dict:
    """How close the calls were, and the setting range that makes no mistake on this recording."""
    out: dict = {}
    if trial.station == "conveyor":
        rows = [v for v in verdicts if v.get("patch_ratio") is not None and "truth" in v]
        good = [v for v in rows if v["truth"] == "same" and v["point_ratio"] >= 1]
        odd = [v for v in rows if v["truth"] == "odd" and v["point_ratio"] >= 1]   # not caught by print points
        if good:
            worst = max(good, key=lambda v: v["patch_ratio"])
            out["good_max_patch"] = {"seq": worst["seq"], "pct": round(worst["patch_ratio"] * 100), "px": worst["patch"]}
            weakest = min((v for v in rows if v["truth"] == "same"), key=lambda v: v["point_ratio"])
            out["good_min_points"] = {"seq": weakest["seq"], "pct": round(weakest["point_ratio"] * 100)}
        if odd:
            nearest = min(odd, key=lambda v: v["patch_ratio"])
            out["odd_min_patch"] = {"seq": nearest["seq"], "pct": round(nearest["patch_ratio"] * 100), "px": nearest["patch"]}
        if rows:
            # limit = larger of diff_min_patch and (patch between the references) x diff_factor
            m = Matchers(trial.dir, settings["matching"]).get(rows[-1]["live"]["refs"])
            out["limit"] = {"px": m.diff_threshold, "baseline": m.baseline_patch,
                            "min_patch": int(settings["matching"]["diff_min_patch"]),
                            "factor": float(settings["matching"]["diff_factor"])}
        if good and odd:
            lo, hi = out["good_max_patch"]["px"], out["odd_min_patch"]["px"]
            out["limit_range"] = [lo, hi] if lo < hi else None
    else:
        right = [v for v in verdicts if v.get("truth") and v["truth"] != "odd" and v.get("pick") == v["truth"]]
        odd = [v for v in verdicts if v.get("truth") == "odd" and v.get("limit_ratio") is not None]
        if right:
            worst = max(right, key=lambda v: v["limit_ratio"])
            out["right_max_limit"] = {"seq": worst["seq"], "pct": round(worst["limit_ratio"] * 100)}
            gaps = [v for v in right if v.get("gap") is not None]
            if gaps:
                tight = min(gaps, key=lambda v: v["gap"])
                out["right_min_gap"] = {"seq": tight["seq"], "gap": tight["gap"]}
        if odd:
            nearest = min(odd, key=lambda v: v["limit_ratio"])
            out["odd_min_limit"] = {"seq": nearest["seq"], "pct": round(nearest["limit_ratio"] * 100)}
    return out


def speed(trial: Trial) -> dict:
    photos = sorted(trial.books + trial.teach + trial.other, key=lambda r: r["seq"])
    out = {"photos": len(photos), "dropped": trial.count("dropped"), "blips": trial.count("blip"),
           "gaps": trial.count("gap"), "unsaved": trial.meta.get("unsaved", 0)}
    times = [r["sensor_t"] for r in photos if r.get("sensor_t") is not None]
    if len(times) > 1:
        steps = [b - a for a, b in zip(times, times[1:]) if b > a]
        out["books_per_hour"] = round((len(times) - 1) / max(times[-1] - times[0], 1e-3) * 3600)
        out["fastest_per_hour"] = round(3600 / min(steps)) if steps else None
    lags = sorted(r["lag_ms"] for r in photos if r.get("lag_ms") is not None)
    if lags:
        out["lag_ms"] = {"median": round(statistics.median(lags)), "p95": round(lags[int(0.95 * (len(lags) - 1))]),
                         "max": round(lags[-1])}
    checks = sorted(r["ms"] for r in trial.books if r.get("ms"))
    if checks:
        out["check_ms"] = {"median": round(statistics.median(checks)),
                           "p95": round(checks[int(0.95 * (len(checks) - 1))]), "max": round(checks[-1])}
    out["max_queued"] = max((r.get("queued") or 0 for r in photos), default=0)
    return out


def changed_vs_live(trial: Trial, verdicts: list[dict]) -> list[int]:
    if trial.station == "conveyor":
        return [v["seq"] for v in verdicts if not v.get("skipped") and v["flag"] != (not v["live"].get("same"))]
    return [v["seq"] for v in verdicts if not v.get("skipped") and v["pick"] != v["live"].get("result")]


# ---- settings ----------------------------------------------------------------------------

def parse_value(section: str, key: str, text: str):
    from .config import DEFAULTS

    default = DEFAULTS.get(section, {}).get(key)
    if isinstance(default, bool):
        return text.lower() in ("1", "true", "yes")
    if isinstance(default, int):
        return int(float(text))
    return float(text)


def with_setting(settings: dict, dotted: str, value) -> dict:
    section, key = dotted.split(".", 1)
    new = copy.deepcopy(settings)
    new.setdefault(section, {})[key] = value
    return new


def scoring_key(settings: dict) -> str:
    matching = {k: v for k, v in settings["matching"].items() if ("matching", k) not in DECISION_KEYS}
    return json.dumps(matching, sort_keys=True)


class Replay:
    def __init__(self, trial: Trial, workers: int | None = None):
        self.trial = trial
        self.workers = workers or os.cpu_count() or 1
        self._scores: dict[str, list] = {}

    def run(self, settings: dict) -> dict:
        key = scoring_key(settings)
        if key not in self._scores:
            self._scores[key] = score_all(self.trial, settings["matching"], self.workers)
        verdicts = decide(self.trial, self._scores[key], settings)
        result = judge(self.trial, verdicts)
        result["verdicts"] = verdicts
        result["margins"] = margins(self.trial, verdicts, settings)
        result["changed"] = changed_vs_live(self.trial, verdicts)
        return result


def draft_truth(trial: Trial, verdicts: list[dict]) -> Path:
    """No truth.txt yet: write what the replay decided, for a person to check and correct."""
    path = trial.dir / "truth.draft.txt"
    labels: dict[int, str] = {}
    comments = {}
    if trial.station == "conveyor":
        for v in verdicts:
            if v.get("flag"):
                labels[v["seq"]] = "odd"
        comments["odd"] = "the station alerted on these; remove any that were really the lot's title"
        note = "Draft from the replay. Add every odd book it missed, then save as truth.txt."
    else:
        unsure = []
        for v in verdicts:
            if v.get("pick"):
                labels[v["seq"]] = v["pick"]
            elif not v.get("skipped"):
                unsure.append(v["seq"])
        comments = {j: trial.names.get(j, "") for j in set(labels.values())}
        note = ("Draft from the replay. Check the contact sheet in report.html, fix wrong titles, and give "
                f"these a line too (not recognised / not sure): {compress(unsure) or 'none'}. Save as truth.txt.")
    write_truth(path, labels, note, comments)
    return path


# ---- HTML report -------------------------------------------------------------------------

def _thumbs(trial: Trial, seqs, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for seq in seqs:
        dst = out / f"{seq:06d}.jpg"
        if dst.exists():
            continue
        img = cv2.imread(str(trial.dir / "photos" / f"{seq:06d}.png"), cv2.IMREAD_GRAYSCALE)
        if img is not None:
            h = round(img.shape[0] * 220 / img.shape[1])
            cv2.imwrite(str(dst), cv2.resize(img, (220, h), interpolation=cv2.INTER_AREA),
                        [cv2.IMWRITE_JPEG_QUALITY, 70])


CSS = """
:root { --bg:#f6f7f9; --fg:#1d232b; --muted:#5b6672; --card:#fff; --line:#dde2e8; --bad:#c62828; --ok:#2e7d32; --warn:#b26a00; }
@media (prefers-color-scheme: dark) { :root { --bg:#14181d; --fg:#e6e9ed; --muted:#9aa5b1; --card:#1d232b; --line:#2c343e; --bad:#ef5350; --ok:#66bb6a; --warn:#ffb74d; } }
body { background:var(--bg); color:var(--fg); font:14px/1.45 system-ui, sans-serif; margin:0; padding:16px; }
h1 { font-size:20px; margin:0 0 4px; } h2 { font-size:16px; margin:24px 0 8px; }
.muted { color:var(--muted); } .bad { color:var(--bad); } .ok { color:var(--ok); } .warn { color:var(--warn); }
.cards { display:flex; flex-wrap:wrap; gap:8px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:8px 12px; min-width:110px; }
.card b { display:block; font-size:22px; font-variant-numeric:tabular-nums; }
table { border-collapse:collapse; background:var(--card); } td, th { border:1px solid var(--line); padding:4px 8px; text-align:right; }
th:first-child, td:first-child { text-align:left; }
.grid { display:flex; flex-wrap:wrap; gap:6px; }
.ph { background:var(--card); border:1px solid var(--line); border-radius:6px; padding:4px; width:220px; font-size:12px; }
.ph img { width:220px; display:block; border-radius:3px; } .ph.mis { border:2px solid var(--bad); }
.ph a { color:inherit; text-decoration:none; }
"""


def _ph(v: dict, station: str, names: dict) -> str:
    seq = v["seq"]
    if station == "conveyor":
        verdict = "ALERT" if v.get("flag") else "ok"
        line = (f"points {v.get('n', '-')}/{v.get('n_limit', '-')} · patch {v.get('patch', '-')}/{v.get('patch_limit', '-')}"
                if v.get("patch_ratio") is not None else f"ncc {v.get('ncc', '-')}")
    else:
        verdict = names.get(v.get("pick"), "rejected") if v.get("pick") else "rejected"
        line = v.get("reason", "")
    truth = v.get("truth")
    truth_txt = f" · truth: {html.escape(names.get(truth, truth))}" if truth else ""
    cls = "ph mis" if v.get("outcome") else "ph"
    return (f'<div class="{cls}"><a href="../photos/{seq:06d}.png"><img loading="lazy" src="thumbs/{seq:06d}.jpg"></a>'
            f'<b>#{seq}</b> {html.escape(verdict)}{truth_txt}<br><span class="muted">{html.escape(line)}</span></div>')


def write_report(trial: Trial, result: dict, settings: dict, sweep: list[tuple[str, dict]] | None = None) -> Path:
    out = trial.dir / "report"
    verdicts = result["verdicts"]
    _thumbs(trial, [v["seq"] for v in verdicts if not v.get("skipped")], out / "thumbs")
    st, names, sp = trial.station, trial.names, speed(trial)
    m = trial.meta
    parts = [f"<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>"
             f"<title>Trial report</title><style>{CSS}</style>",
             f"<h1>{html.escape(m.get('job_no', ''))} · {html.escape(m.get('job_name', ''))}</h1>",
             f"<div class='muted'>{st} station · {html.escape(m.get('mode', ''))} · {m.get('started', '')} to "
             f"{m.get('ended', 'still open')}</div>"]

    cards = [("Books checked", len(verdicts))]
    if trial.truth is None:
        parts.append("<p class='warn'>No truth.txt yet, so mistakes cannot be counted. A draft was written to "
                     "truth.draft.txt: check it, correct it and save it as truth.txt, then run the replay again.</p>")
    elif st == "conveyor":
        cards += [("Missed odd books", result["missed"]), ("False alarms", result["false_alarm"]),
                  ("Odd books caught", result["caught"]), ("Good books passed", result["passed"])]
    else:
        cards += [("Counted as wrong title", result["wrong_title"]), ("Foreign book counted", result["foreign_counted"]),
                  ("Good book rejected", result["rejected"]), ("Foreign caught", result["caught"]),
                  ("Right title", result["right"])]
    cards += [("Not scored", result["unscored"]), ("Changed vs live", len(result["changed"]))]
    parts.append("<div class='cards'>" + "".join(
        f"<div class='card'><b class='{'bad' if ('Missed' in k or 'False' in k or 'wrong' in k or 'counted' in k) and val else ''}'>"
        f"{val}</b>{k}</div>" for k, val in cards) + "</div>")

    parts.append("<h2>Speed and sensor</h2><div class='cards'>" + "".join(
        f"<div class='card'><b>{val}</b>{k}</div>" for k, val in [
            ("Photos", sp["photos"]), ("Books/hour", sp.get("books_per_hour", "-")),
            ("Fastest pace /hr", sp.get("fastest_per_hour", "-")), ("Dropped photos", sp["dropped"]),
            ("Noise blips", sp["blips"]), ("Gap alarms", sp["gaps"]), ("Not saved", sp["unsaved"]),
            ("Check ms (median / max)", f"{sp['check_ms']['median']} / {sp['check_ms']['max']}" if "check_ms" in sp else "-"),
            ("Sensor→done ms (p95)", sp["lag_ms"]["p95"] if "lag_ms" in sp else "-"),
            ("Most waiting", sp["max_queued"])]) + "</div>")

    mg = result["margins"]
    if mg:
        rows = []
        if st == "conveyor":
            if "good_max_patch" in mg:
                rows.append(f"Good books reach at most <b>{mg['good_max_patch']['pct']}%</b> of the changed-patch limit (#{mg['good_max_patch']['seq']}).")
                rows.append(f"Weakest good book has <b>{mg['good_min_points']['pct']}%</b> of the print points needed (#{mg['good_min_points']['seq']}).")
            if "odd_min_patch" in mg:
                rows.append(f"The closest odd book (not caught by print points) reaches <b>{mg['odd_min_patch']['pct']}%</b> of the limit (#{mg['odd_min_patch']['seq']}).")
            if "limit" in mg:
                rows.append(limit_advice(mg))
        else:
            if "right_max_limit" in mg:
                rows.append(f"Correctly named books reach at most <b>{mg['right_max_limit']['pct']}%</b> of the wrong-book limit (#{mg['right_max_limit']['seq']}).")
            if "right_min_gap" in mg:
                rows.append(f"Tightest win over the runner-up title: <b>{mg['right_min_gap']['gap']}</b> px (needs {settings['packing']['min_patch_gap']}; #{mg['right_min_gap']['seq']}).")
            if "odd_min_limit" in mg:
                rows.append(f"The closest foreign book reaches <b>{mg['odd_min_limit']['pct']}%</b> of the wrong-book limit (#{mg['odd_min_limit']['seq']}).")
        parts.append("<h2>Safety margins</h2><ul>" + "".join(f"<li>{r}</li>" for r in rows) + "</ul>")

    if sweep:
        parts.append("<h2>Settings tried</h2>" + sweep_table_html(trial.station, sweep))

    if result["mistakes"]:
        parts.append(f"<h2 class='bad'>Mistakes ({len(result['mistakes'])})</h2><div class='grid'>" +
                     "".join(_ph(v, st, names) for v in result["mistakes"]) + "</div>")
    if result["changed"]:
        parts.append(f"<h2>Decided differently from the live run ({len(result['changed'])})</h2>"
                     f"<p class='muted'>Photos {compress(result['changed'])}</p>")

    scored = [v for v in verdicts if not v.get("skipped")]
    if st == "conveyor":
        scored.sort(key=lambda v: -(v.get("patch_ratio") if v.get("patch_ratio") is not None else (1 - v.get("ncc", 1))))
        parts.append("<h2>All photos, most different first</h2><p class='muted'>Odd books should all be at the top. "
                     "A good book among them is a near false alarm; an odd book further down is a near miss.</p>"
                     "<div class='grid'>" + "".join(_ph(v, st, names) for v in scored) + "</div>")
    else:
        groups: dict[str, list] = {}
        for v in scored:
            groups.setdefault(v.get("pick") or "", []).append(v)
        for key in sorted(groups, key=lambda k: (k == "", names.get(k, k))):
            title = names.get(key, key) if key else "Rejected (not recognised / not sure)"
            parts.append(f"<h2>{html.escape(title)} ({len(groups[key])})</h2>"
                         "<div class='grid'>" + "".join(_ph(v, st, names) for v in groups[key]) + "</div>")
    path = out / "report.html"
    path.write_text("\n".join(parts), encoding="utf-8")
    return path


def limit_advice(mg: dict) -> str:
    """Plain words on the changed-patch limit: what sets it now, and the range that made no mistake."""
    lim = mg["limit"]
    if lim["baseline"] * lim["factor"] > lim["min_patch"]:
        now = (f"The changed-patch limit is {lim['px']} px (patch between the references {lim['baseline']} "
               f"x diff_factor {lim['factor']:g}).")
        knob = "diff_factor"
    else:
        now = (f"The changed-patch limit is {lim['px']} px, set by diff_min_patch: the references differ by only "
               f"{lim['baseline']} px, so diff_factor has no effect.")
        knob = "diff_min_patch"
    if "limit_range" not in mg:
        return now + " Add odd books from the same series to the trial to see how far it can move."
    if mg["limit_range"] is None:
        return now + (" <span class='bad'>No limit separates good and odd books here: the closest odd book changed "
                      "less than the worst good book. Check those two photos (light, focus, position).</span>")
    lo, hi = mg["limit_range"]
    return now + (f" Any limit above {lo} px and below {hi} px made no patch mistake on this recording; "
                  f"aim near the middle (about {(lo + hi) // 2} px) by changing {knob}.")


def sweep_row(station: str, result: dict) -> list:
    if "scored" not in result or not result["scored"]:
        return ["-"] * (4 if station == "conveyor" else 5)
    if station == "conveyor":
        return [result["missed"], result["false_alarm"], result["caught"], result["passed"]]
    return [result["wrong_title"], result["foreign_counted"], result["rejected"], result["caught"], result["right"]]


def sweep_header(station: str) -> list[str]:
    if station == "conveyor":
        return ["Missed odd", "False alarms", "Caught", "Passed"]
    return ["Wrong title counted", "Foreign counted", "Good rejected", "Foreign caught", "Right"]


def sweep_table_html(station: str, sweep: list[tuple[str, dict]]) -> str:
    head = "".join(f"<th>{h}</th>" for h in ["Setting"] + sweep_header(station))
    rows = "".join("<tr>" + "".join(f"<td>{html.escape(str(c))}</td>" for c in [label] + sweep_row(station, r)) + "</tr>"
                   for label, r in sweep)
    return f"<table><tr>{head}</tr>{rows}</table>"
