"""Settings: config.yaml, then config.local.yaml (this Pi only, not in git), then
data/overrides.json (changes made on the touchscreen)."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import yaml

DEFAULTS = {
    "station": "conveyor",          # conveyor | packing
    "station_name": "Conveyor 1",
    "mock": False,
    "data_dir": "data",
    "camera": {"type": "picamera", "usb_index": 0, "width": 1280, "height": 800, "fps": 120,
               "exposure_us": 400, "gain": 4.0, "autofocus": False, "rotate": 0, "crop": None},
    "sensor": {"pin": 17, "active_low": True, "capture_delay_ms": 60, "min_pulse_ms": 15,
               "max_pulse_ms": 900},
    "matching": {"reference_count": 5, "work_width": 640, "orb_features": 800, "min_inliers": 15,
                 "inlier_ratio": 0.35, "diff_level": 50, "diff_min_patch": 100, "diff_factor": 3.0,
                 "min_keypoints": 60, "ncc_threshold": 0.6},
    "alarm": {"buzzer_pin": None, "alarm_seconds": 2},
    "storage": {"save_ok_every": 200, "jpeg_quality": 85},
    "trial": {"record": False, "folder": "trials"},   # record every photo for scripts/replay.py
    "erp": {"enabled": True, "server": "cdcindas.24mycloud.com", "port": 51175,
            "database": "IndusEnterprise"},
    "packing": {"reference_count": 3, "label_scan_ms": 300, "label_confirm_reads": 2,
                "patch_factor": 2.0, "min_patch_gap": 20, "photo_width": 1280, "conveyor_url": None},
    "mock_settings": {"books_per_hour": 6000, "odd_book_every": 40, "books_per_title": 4,
                      "seconds_per_book": 0.6},
}

# Settings the operator may change on the touchscreen: (section, key, label, step, min, max, unit)
TUNABLE = {
    "conveyor": [
        ("sensor", "capture_delay_ms", "Photo delay after sensor", 5, 0, 500, "ms"),
        ("camera", "exposure_us", "Exposure", 50, 50, 5000, "µs"),
        ("camera", "gain", "Brightness gain", 0.5, 1.0, 16.0, "x"),
        ("sensor", "max_pulse_ms", "Gap alarm after", 50, 100, 5000, "ms"),
        ("matching", "inlier_ratio", "Print match needed", 0.05, 0.1, 0.9, ""),
        ("matching", "diff_min_patch", "Changed patch allowed", 25, 25, 5000, "px"),
    ],
    "packing": [
        ("sensor", "capture_delay_ms", "Photo delay after sensor", 10, 0, 1500, "ms"),
        ("camera", "exposure_us", "Exposure (0 = auto)", 250, 0, 20000, "µs"),
        ("camera", "gain", "Brightness gain", 0.5, 1.0, 16.0, "x"),
        ("matching", "inlier_ratio", "Print match needed", 0.05, 0.1, 0.9, ""),
        ("packing", "patch_factor", "Cover difference allowed", 0.5, 1.0, 20.0, "x"),
    ],
}


def _merge(base: dict, extra: dict) -> dict:
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value
    return base


def load_env(path: Path) -> None:
    """Minimal .env reader so the ERP login stays out of config.yaml."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


class Config:
    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        self.root = self.path.parent
        load_env(self.root / ".env")

        values = copy.deepcopy(DEFAULTS)
        if self.path.exists():
            _merge(values, yaml.safe_load(self.path.read_text(encoding="utf-8")) or {})
        local = self.root / "config.local.yaml"   # e.g. this Pi's camera crop; git pull never overwrites it
        if local.exists():
            _merge(values, yaml.safe_load(local.read_text(encoding="utf-8")) or {})
        self.data_dir = (self.root / values["data_dir"]).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.overrides_path = self.data_dir / "overrides.json"
        self.overrides = {}
        if self.overrides_path.exists():
            self.overrides = json.loads(self.overrides_path.read_text(encoding="utf-8"))
        _merge(values, copy.deepcopy(self.overrides))
        self.values = values

    def __getitem__(self, key):
        return self.values[key]

    def tunables(self) -> list[dict]:
        return [{"section": s, "key": k, "label": label, "step": step, "min": lo, "max": hi,
                 "unit": unit, "value": self.values[s][k]}
                for s, k, label, step, lo, hi, unit in TUNABLE[self.values["station"]]]

    def update(self, changes: dict) -> dict:
        """Apply {"section": {"key": value}} for tunable keys only; returns what was applied."""
        allowed = {(s, k): (lo, hi) for s, k, _, _, lo, hi, _ in TUNABLE[self.values["station"]]}
        applied: dict = {}
        for section, items in changes.items():
            for key, value in items.items():
                if (section, key) not in allowed:
                    continue
                lo, hi = allowed[(section, key)]
                value = min(max(float(value), lo), hi)
                if isinstance(DEFAULTS[section][key], int):
                    value = int(round(value))
                applied.setdefault(section, {})[key] = value
        _merge(self.overrides, applied)
        _merge(self.values, copy.deepcopy(applied))
        self.overrides_path.write_text(json.dumps(self.overrides, indent=2), encoding="utf-8")
        return applied
