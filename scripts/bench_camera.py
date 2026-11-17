"""Camera check: real frame rate, sharpness and brightness at a given exposure; saves a photo.

Run on the Pi:  python3 scripts/bench_camera.py --exposure 400 --gain 4 --out test.jpg
Sharpness (Laplacian variance) should not drop much between a still book and a
moving one; if it does, shorten the exposure and add light.
"""
import argparse
import sys
import time
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.camera import PiCamera  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--exposure", type=int, default=400, help="µs")
ap.add_argument("--gain", type=float, default=4.0)
ap.add_argument("--fps", type=int, default=120)
ap.add_argument("--out", default="test.jpg")
args = ap.parse_args()

cam = PiCamera({"width": 1280, "height": 800, "fps": args.fps, "exposure_us": args.exposure, "gain": args.gain})
time.sleep(1)
n, t0 = 0, time.monotonic()
while time.monotonic() - t0 < 2:
    frame = cam.grab()
    n += 1
fps = n / (time.monotonic() - t0)
sharp = cv2.Laplacian(frame, cv2.CV_64F).var()
print(f"frame rate {fps:.0f} fps   brightness {frame.mean():.0f}/255   sharpness {sharp:.0f}")
if frame.mean() < 60:
    print("Too dark: add light or raise gain (exposure must stay short for moving books).")
cv2.imwrite(args.out, frame)
print(f"saved {args.out}")
cam.close()
