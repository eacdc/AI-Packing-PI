"""Re-run recorded trials with any settings and count the mistakes.

Record first:  python -m app --record      (or trial: {record: true} in the config)
Every lot is saved under data/trials/<date>_<job>_<mode>/. Write truth.txt there (which
photos were really odd books; see the help at the top of truth.draft.txt), then:

  python scripts/replay.py data/trials/20261120_101500_J03704_26_27_page
      re-run with the settings the trial was recorded with; writes report/report.html
  python scripts/replay.py data/trials/*  --config config.yaml
      several trials, with this station's current settings (config + touchscreen changes)
  python scripts/replay.py <trial> --set matching.diff_factor=2.5 --set matching.inlier_ratio=0.3
  python scripts/replay.py <trial> --sweep matching.diff_factor=2,2.5,3,3.5,4
      try several values; the table shows the mistakes for each

Limits (diff_factor, diff_min_patch, patch_factor, min_patch_gap) sweep instantly; other
settings re-check every photo, which takes a while on a Pi (better on a PC: copy the folder).
"""
import argparse
import glob
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.replay import (Replay, Trial, draft_truth, limit_advice, parse_value, speed,  # noqa: E402
                        sweep_header, sweep_row, with_setting, write_report)
from app.trial import compress  # noqa: E402


def settings_for(trial: Trial, args) -> dict:
    settings = {k: dict(v) for k, v in trial.meta["settings"].items()}
    if args.config:
        from app.config import Config

        cfg = Config(args.config)
        for section in ("matching", "packing"):
            settings[section] = dict(cfg[section])
    for item in args.set:
        dotted, value = item.split("=", 1)
        section, key = dotted.split(".", 1)
        settings = with_setting(settings, dotted, parse_value(section, key, value))
    return settings


def print_result(trial: Trial, result: dict) -> None:
    if trial.truth is None:
        print("  no truth.txt: mistakes not counted")
    elif trial.station == "conveyor":
        print(f"  scored {result['scored']}: missed odd books {result['missed']}, false alarms {result['false_alarm']}, "
              f"caught {result['caught']}, passed {result['passed']}")
    else:
        print(f"  scored {result['scored']}: wrong title counted {result['wrong_title']}, foreign counted "
              f"{result['foreign_counted']}, good rejected {result['rejected']}, foreign caught {result['caught']}, "
              f"right {result['right']}")
    for v in result["mistakes"][:15]:
        print(f"    #{v['seq']}: {v['outcome'].replace('_', ' ')} - truth {v['truth']}, {v['reason']}")
    if len(result["mistakes"]) > 15:
        print(f"    ... {len(result['mistakes']) - 15} more in the report")
    if result["changed"]:
        print(f"  decided differently from the live run: {compress(result['changed'])}")
    mg = result["margins"]
    if "limit" in mg:
        print("  " + re.sub(r"<[^>]+>", "", limit_advice(mg)))
    if "right_min_gap" in mg:
        print(f"  tightest win over the runner-up title: {mg['right_min_gap']['gap']} px (#{mg['right_min_gap']['seq']}); "
              "min_patch_gap must stay below this")
    if "right_max_limit" in mg:
        print(f"  correctly named books reach at most {mg['right_max_limit']['pct']}% of the wrong-book limit "
              f"(#{mg['right_max_limit']['seq']})")
    if "odd_min_limit" in mg:
        print(f"  closest foreign book: {mg['odd_min_limit']['pct']}% of the wrong-book limit (#{mg['odd_min_limit']['seq']})")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("trials", nargs="+", help="trial folder(s) under data/trials/")
    ap.add_argument("--config", help="use this config's matching/packing settings instead of the recorded ones")
    ap.add_argument("--set", action="append", default=[], metavar="section.key=value")
    ap.add_argument("--sweep", metavar="section.key=v1,v2,...")
    ap.add_argument("--workers", type=int, default=None, help="CPU cores to use (default: all)")
    ap.add_argument("--no-report", action="store_true")
    args = ap.parse_args()

    folders = [Path(p) for pattern in args.trials for p in (glob.glob(pattern) or [pattern])]
    folders = [f for f in folders if (f / "trial.json").exists()]
    if not folders:
        sys.exit("no trial folders found (each has a trial.json)")

    totals: dict[str, dict] = {}
    for folder in folders:
        trial = Trial(folder)
        base = settings_for(trial, args)
        replay = Replay(trial, args.workers)
        sp = speed(trial)
        print(f"\n{folder.name}: {trial.station}, {len(trial.books)} books checked, {len(trial.teach)} teaching photos, "
              f"{sp.get('books_per_hour', '-')} books/hr, {sp['dropped']} dropped, {sp['gaps']} gap alarms")
        result = replay.run(base)
        print_result(trial, result)

        sweep = None
        if args.sweep:
            dotted, values = args.sweep.split("=", 1)
            section, key = dotted.split(".", 1)
            sweep = []
            for text in values.split(","):
                label = f"{dotted}={text}"
                r = replay.run(with_setting(base, dotted, parse_value(section, key, text)))
                sweep.append((label, r))
                tot = totals.setdefault(label, {"station": trial.station, "rows": []})
                tot["rows"].append(sweep_row(trial.station, r))
            print("  " + " | ".join(["setting".ljust(34)] + sweep_header(trial.station)))
            for label, r in sweep:
                print("  " + " | ".join([label.ljust(34)] + [str(c).rjust(len(h)) for c, h in
                                                             zip(sweep_row(trial.station, r), sweep_header(trial.station))]))
        if trial.truth is None:
            print(f"  wrote {draft_truth(trial, result['verdicts'])}")
        if not args.no_report:
            print(f"  report: {write_report(trial, result, base, sweep)}")

    if args.sweep and len(folders) > 1:
        print("\nAll trials together (trials with a truth.txt):")
        for label, tot in totals.items():
            rows = [r for r in tot["rows"] if r[0] != "-"]
            sums = [sum(col) for col in zip(*rows)] if rows else []
            print(f"  {label.ljust(34)} " + " | ".join(f"{h} {s}" for h, s in zip(sweep_header(tot["station"]), sums)))


if __name__ == "__main__":
    main()
