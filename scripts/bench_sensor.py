"""Bench / line test for the photo sensor through the optocoupler.

Prints every book (pulse) with its length and the gap before it, and every 20
books a summary: books per hour, shortest gap, shortest and longest pulse.
Run on the Pi:  python3 scripts/bench_sensor.py --pin 17
"""
import argparse
import signal
import time

from gpiozero import DigitalInputDevice

ap = argparse.ArgumentParser()
ap.add_argument("--pin", type=int, default=17, help="BCM GPIO number")
ap.add_argument("--active-high", action="store_true", help="input is high while the sensor is ON")
args = ap.parse_args()

dev = DigitalInputDevice(args.pin, pull_up=not args.active_high)
state = {"on": None, "off": None, "starts": [], "gaps": [], "pulses": []}


def book_on():
    t = time.monotonic()
    gap = (t - state["off"]) * 1000 if state["off"] else None
    state["on"] = t
    state["starts"].append(t)
    if gap is not None:
        state["gaps"].append(gap)


def book_off():
    t = time.monotonic()
    state["off"] = t
    if state["on"] is None:
        return
    pulse = (t - state["on"]) * 1000
    state["pulses"].append(pulse)
    n = len(state["pulses"])
    gap = f"{state['gaps'][-1]:6.1f} ms" if state["gaps"] else "   -    "
    print(f"book {n:5d}   on {pulse:6.1f} ms   gap before {gap}")
    if n % 20 == 0:
        starts = state["starts"][-20:]
        rate = (len(starts) - 1) / (starts[-1] - starts[0]) * 3600 if len(starts) > 1 else 0
        gaps = state["gaps"][-20:] or [0]
        pulses = state["pulses"][-20:]
        print(f"--- last 20: {rate:,.0f} books/hr   shortest gap {min(gaps):.1f} ms   "
              f"pulse {min(pulses):.1f}-{max(pulses):.1f} ms ---")


dev.when_activated = book_on
dev.when_deactivated = book_off
print(f"Watching GPIO{args.pin} ({'active high' if args.active_high else 'active low'}). "
      f"Now: {'ON' if dev.is_active else 'OFF'}. Ctrl+C to stop.")
signal.pause()
