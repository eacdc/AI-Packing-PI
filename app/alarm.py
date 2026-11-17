"""Optional buzzer / tower light on a GPIO pin (through a relay or transistor)."""
from __future__ import annotations


class Alarm:
    def __init__(self, cfg: dict, mock: bool):
        self.seconds = float(cfg.get("alarm_seconds", 2))
        self.dev = None
        pin = cfg.get("buzzer_pin")
        if pin is not None and not mock:
            from gpiozero import DigitalOutputDevice

            self.dev = DigitalOutputDevice(pin)

    def trigger(self) -> None:
        if self.dev is not None:
            self.dev.blink(on_time=self.seconds, off_time=0.1, n=1, background=True)

    def close(self) -> None:
        if self.dev is not None:
            self.dev.close()
