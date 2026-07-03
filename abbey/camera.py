"""
camera.py — Smooth live feed + burst capture + motion awareness.

Why a thread: reading frames on the UI thread makes the feed stutter. A
background reader keeps the newest frame ready so the display stays smooth while
staff step back and slowly spin the item.

Capture model (matches how staff actually work):
  1. Live feed runs continuously and smoothly.
  2. Staff present the item; motion rises.
  3. Staff step back / slowly rotate it.
  4. On "Capture" (button or, if auto_capture is on, when motion settles) we grab
     a BURST of frames across `burst_seconds`, covering the rotation.
  5. vision.rank_frames picks the best few; the sharpest well-framed shot becomes
     the hero, the rest are offered as alternates.

Requires a physical camera, so this module is syntax-checked here and exercised
on the desk machine. The pure scoring it relies on is fully tested in
tests/test_vision.py.
"""

from __future__ import annotations

import threading
import time

import cv2
import numpy as np

from . import vision


class CameraStream:
    """Background-threaded camera reader for a stutter-free preview."""

    def __init__(self, index: int = 0, width: int = 1920, height: int = 1080):
        self.index = index
        self.width = width
        self.height = height
        self.cap: cv2.VideoCapture | None = None
        self._frame: np.ndarray | None = None
        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self) -> "CameraStream":
        self.cap = cv2.VideoCapture(self.index)
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        # Small buffer keeps the preview "live" rather than lagging behind.
        try:
            self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except cv2.error:
            pass
        self._running = True
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()
        return self

    def _reader(self) -> None:
        while self._running and self.cap is not None:
            ok, frame = self.cap.read()
            if ok:
                with self._lock:
                    self._frame = frame
            else:
                time.sleep(0.01)

    def read(self) -> np.ndarray | None:
        with self._lock:
            return None if self._frame is None else self._frame.copy()

    def is_opened(self) -> bool:
        return bool(self.cap and self.cap.isOpened())

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)
        if self.cap:
            self.cap.release()
        self.cap = None


def capture_burst(stream: CameraStream, count: int, seconds: float) -> list[np.ndarray]:
    """Grab `count` frames spread evenly over `seconds` (so a slow spin is covered)."""
    frames: list[np.ndarray] = []
    if count <= 0:
        return frames
    interval = seconds / count
    for _ in range(count):
        f = stream.read()
        if f is not None:
            frames.append(f)
        time.sleep(interval)
    return frames


def best_shots(frames: list[np.ndarray], keep_n: int, weights: dict) -> tuple[np.ndarray | None, list[np.ndarray]]:
    """Return (hero_frame, [alternates]) using vision ranking."""
    if not frames:
        return None, []
    idxs = vision.pick_best(frames, k=min(keep_n, len(frames)), **weights)
    ordered = [frames[i] for i in idxs]
    return ordered[0], ordered[1:]


class MotionGate:
    """Tracks motion to support auto-capture: fire once the item has been
    presented (motion rose) and then settled (motion fell)."""

    def __init__(self, active_threshold: float, still_threshold: float,
                 settle_frames: int = 8):
        self.active_threshold = active_threshold
        self.still_threshold = still_threshold
        self.settle_frames = settle_frames
        self._prev: np.ndarray | None = None
        self._was_active = False
        self._still_count = 0

    def update(self, frame: np.ndarray) -> bool:
        """Feed the newest frame. Returns True on the moment we should auto-capture."""
        if self._prev is None:
            self._prev = frame
            return False
        m = vision.motion_level(self._prev, frame)
        self._prev = frame
        if m > self.active_threshold:
            self._was_active = True
            self._still_count = 0
            return False
        if self._was_active and m < self.still_threshold:
            self._still_count += 1
            if self._still_count >= self.settle_frames:
                self._was_active = False
                self._still_count = 0
                return True
        return False
