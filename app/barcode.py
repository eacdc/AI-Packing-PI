"""Reads the packing-slip label from a camera photo.

The label barcode is Code 39 of PackingSlipBarcodeEntry.BarcodeNo, zero-padded to
7 digits (e.g. 0716919). The reader needs about 2 pixels per narrow bar, so the
label must be shown close enough to the camera.
"""
from __future__ import annotations

import numpy as np


def read_label_numbers(gray: np.ndarray) -> list[int]:
    import zxingcpp

    numbers = []
    for result in zxingcpp.read_barcodes(gray, formats=zxingcpp.BarcodeFormat.Code39):
        text = result.text.strip().strip("*")
        if text.isdigit():
            numbers.append(int(text))
    return numbers


def render_label(number: int, scale: int = 3) -> np.ndarray:
    """A grayscale label image as printed (for mock mode and tests)."""
    import cv2
    import zxingcpp

    code = zxingcpp.create_barcode(f"{number:07d}", zxingcpp.BarcodeFormat.Code39)
    bars = np.array(zxingcpp.write_barcode_to_image(code, scale=scale))
    h, w = bars.shape[:2]
    label = np.full((h + 90, w + 40), 255, np.uint8)
    label[20:20 + h, 20:20 + w] = bars if bars.ndim == 2 else bars[..., 0]
    cv2.putText(label, f"{number:07d}", (20, h + 70), cv2.FONT_HERSHEY_SIMPLEX, 1.2, 0, 2)
    return label
