"""Autonics photo sensor -> PC817 optocoupler -> Pi GPIO.

The sensor's 24 V output never touches the Pi: the optocoupler output pulls the
GPIO low (active_low) while a book is in front of the sensor.
"""
from __future__ import annotations

import time
from typing import Callable


class SensorTrigger:
    def __init__(self, cfg: dict):
        from gpiozero import DigitalInputDevice

        # pull_up=True makes the input active when low; pull_up=False active when high.
        self.dev = DigitalInputDevice(cfg["pin"], pull_up=bool(cfg["active_low"]))

    def start(self, on_book_start: Callable[[float], None], on_book_end: Callable[[float], None]) -> None:
        self.dev.when_activated = lambda: on_book_start(time.monotonic())
        self.dev.when_deactivated = lambda: on_book_end(time.monotonic())

    def close(self) -> None:
        self.dev.close()
