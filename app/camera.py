"""Cameras.

PiCamera: any Pi ribbon camera through Picamera2 (Raspberry Pi OS Bookworm) -
the OV9281 global-shutter camera at the conveyor, or a Camera Module 3 at packing.
It streams continuously; grab() returns the next frame, so after the sensor fires
the photo is at most one frame period late.

UsbCamera: a USB webcam through OpenCV, for a packing station without a ribbon camera.
"""
from __future__ import annotations

import threading

import numpy as np


class PiCamera:
    def __init__(self, cfg: dict):
        from picamera2 import Picamera2

        self.width, self.height = cfg["width"], cfg["height"]
        frame_us = int(1_000_000 / cfg["fps"])
        controls = {"FrameDurationLimits": (frame_us, frame_us)}
        if cfg["exposure_us"]:
            controls.update(AeEnable=False, ExposureTime=int(cfg["exposure_us"]), AnalogueGain=float(cfg["gain"]))
        self.cam = Picamera2()
        config = self.cam.create_video_configuration(
            main={"size": (self.width, self.height), "format": "YUV420"},
            controls=controls,
            buffer_count=4,
        )
        self.cam.configure(config)
        self.cam.start()
        if cfg.get("autofocus") and "AfMode" in self.cam.camera_controls:
            self.cam.set_controls({"AfMode": 2})  # continuous autofocus (Camera Module 3)

    def set_exposure(self, exposure_us: int, gain: float) -> None:
        if exposure_us:
            self.cam.set_controls({"AeEnable": False, "ExposureTime": int(exposure_us), "AnalogueGain": float(gain)})
        else:
            self.cam.set_controls({"AeEnable": True})

    def grab(self) -> np.ndarray:
        """Next frame as an 8-bit grayscale image (the Y plane)."""
        yuv = self.cam.capture_array("main")
        return yuv[: self.height, : self.width].copy()

    def close(self) -> None:
        self.cam.stop()
        self.cam.close()


class UsbCamera:
    def __init__(self, cfg: dict):
        import cv2

        self.cv2 = cv2
        self.cap = cv2.VideoCapture(int(cfg.get("usb_index", 0)))
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, cfg["width"])
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg["height"])
        self.lock = threading.Lock()
        if not self.cap.isOpened():
            raise RuntimeError("USB camera not found")

    def set_exposure(self, exposure_us: int, gain: float) -> None:
        pass  # left to the webcam's own auto exposure

    def grab(self) -> np.ndarray:
        with self.lock:
            self.cap.grab()  # drop the buffered frame so the photo is fresh
            ok, frame = self.cap.read()
        if not ok:
            raise RuntimeError("USB camera read failed")
        return self.cv2.cvtColor(frame, self.cv2.COLOR_BGR2GRAY)

    def close(self) -> None:
        self.cap.release()


def open_camera(cfg: dict):
    return UsbCamera(cfg) if cfg.get("type") == "usb" else PiCamera(cfg)
