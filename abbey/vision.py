"""
vision.py — Image quality scoring and best-frame selection.

This is the "pick the best photo" brain. When staff spin an item in front of the
camera we grab a burst of frames; this module scores each one for sharpness,
exposure, contrast and how well the subject fills the frame, then ranks them so
Abbey can show the best few and auto-pick a hero shot.

Everything here is pure (numpy arrays in, numbers out) so it is fully unit-tested
in tests/test_vision.py without needing a live camera.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

# Reference sharpness (Laplacian variance) that counts as "tack sharp" for a
# 1080p furniture shot. Used only for an absolute 0..1 read; ranking within a
# burst is done by relative normalisation which is far more robust.
SHARP_REFERENCE = 550.0


def _ensure_gray(image: np.ndarray) -> np.ndarray:
    if image.ndim == 2:
        return image
    return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(x)))


# ---------------------------------------------------------------------------
# Individual metrics
# ---------------------------------------------------------------------------
def sharpness(image: np.ndarray) -> float:
    """Raw focus measure: variance of the Laplacian. Higher = sharper."""
    gray = _ensure_gray(image)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def exposure_score(image: np.ndarray) -> float:
    """0..1. Rewards a mid-toned image; punishes blown-out or crushed frames."""
    gray = _ensure_gray(image).astype(np.float32)
    mean = gray.mean() / 255.0
    # Gaussian centred on 0.5 (a nicely exposed frame sits mid-histogram).
    mean_term = float(np.exp(-((mean - 0.5) ** 2) / (2 * 0.20 ** 2)))
    clip_low = float((gray < 8).mean())
    clip_high = float((gray > 247).mean())
    clip_penalty = 1.0 - min(1.0, (clip_low + clip_high) * 3.0)
    return _clamp(mean_term * clip_penalty)


def contrast_score(image: np.ndarray) -> float:
    """0..1. Standard deviation of luminance, normalised. Flat photos score low."""
    gray = _ensure_gray(image).astype(np.float32)
    return _clamp(min(gray.std() / 64.0, 1.0))


def subject_fill_score(image: np.ndarray) -> float:
    """0..1. Estimates whether a textured subject sits in the centre of frame.

    We compare edge density in the central 60% against the whole frame. An item
    presented to the camera concentrates edges centrally; an empty bench does
    not. Cheap, lighting-tolerant, and good enough to reject "staff walked away
    and the item's gone" frames.
    """
    gray = _ensure_gray(image)
    edges = cv2.Canny(gray, 50, 150)
    h, w = edges.shape
    cy0, cy1 = int(h * 0.20), int(h * 0.80)
    cx0, cx1 = int(w * 0.20), int(w * 0.80)
    center = float(edges[cy0:cy1, cx0:cx1].mean())
    overall = float(edges.mean()) + 1e-6
    ratio = center / overall
    return _clamp(min(ratio / 1.5, 1.0))


def motion_level(prev: np.ndarray, curr: np.ndarray) -> float:
    """0..1 mean absolute frame difference. ~0 = still, higher = movement.

    Used to know when the item has settled (staff stepped back) so we can fire a
    burst, and to detect when a new item has been presented.
    """
    g0 = _ensure_gray(prev).astype(np.float32)
    g1 = _ensure_gray(curr).astype(np.float32)
    if g0.shape != g1.shape:
        g1 = cv2.resize(g1, (g0.shape[1], g0.shape[0]))
    return float(np.abs(g0 - g1).mean()) / 255.0


# ---------------------------------------------------------------------------
# Composite + ranking
# ---------------------------------------------------------------------------
@dataclass
class FrameScore:
    index: int
    composite: float
    sharpness: float
    exposure: float
    contrast: float
    subject: float


def raw_metrics(image: np.ndarray) -> dict:
    return {
        "sharpness": sharpness(image),
        "exposure": exposure_score(image),
        "contrast": contrast_score(image),
        "subject": subject_fill_score(image),
    }


def rank_frames(
    frames: list[np.ndarray],
    w_sharpness: float = 0.45,
    w_exposure: float = 0.25,
    w_contrast: float = 0.15,
    w_subject: float = 0.15,
) -> list[FrameScore]:
    """Score and rank a burst. Sharpness is min-max normalised *within the burst*
    (robust to scene/lighting), the other metrics are already 0..1.

    Returns a list of FrameScore sorted best-first.
    """
    if not frames:
        return []

    metrics = [raw_metrics(f) for f in frames]
    sharp_vals = np.array([m["sharpness"] for m in metrics], dtype=np.float64)
    lo, hi = float(sharp_vals.min()), float(sharp_vals.max())
    span = (hi - lo) or 1.0

    scores: list[FrameScore] = []
    for i, m in enumerate(metrics):
        s_norm = (m["sharpness"] - lo) / span            # 0..1 within the burst
        composite = (
            w_sharpness * s_norm
            + w_exposure * m["exposure"]
            + w_contrast * m["contrast"]
            + w_subject * m["subject"]
        )
        scores.append(
            FrameScore(
                index=i,
                composite=float(composite),
                sharpness=s_norm,
                exposure=m["exposure"],
                contrast=m["contrast"],
                subject=m["subject"],
            )
        )

    scores.sort(key=lambda s: s.composite, reverse=True)
    return scores


def pick_best(frames: list[np.ndarray], k: int = 1, **weights) -> list[int]:
    """Return the indices of the best `k` frames (best first)."""
    ranked = rank_frames(frames, **weights)
    return [fs.index for fs in ranked[:k]]


def absolute_sharpness_ok(image: np.ndarray, floor: float = 120.0) -> bool:
    """Quick gate: is a single frame sharp enough to bother with at all?"""
    return sharpness(image) >= floor


# ---------------------------------------------------------------------------
# "Look closer" — tiled high-detail analysis
# ---------------------------------------------------------------------------
def tile_image(image: np.ndarray, grid: int = 2, upscale: float = 2.0) -> list[np.ndarray]:
    """Cut an image into a grid×grid set of tiles and upscale each one, so small
    detail (maker's marks, model numbers, signatures, hallmarks) is larger and
    clearer when analysed. Returns tiles left-to-right, top-to-bottom.

    Pure numpy/OpenCV — unit-tested on shapes.
    """
    if grid < 1:
        grid = 1
    h, w = image.shape[:2]
    th, tw = h // grid, w // grid
    tiles: list[np.ndarray] = []
    for r in range(grid):
        for c in range(grid):
            y0, x0 = r * th, c * tw
            # last row/col takes the remainder so nothing is dropped
            y1 = h if r == grid - 1 else y0 + th
            x1 = w if c == grid - 1 else x0 + tw
            tile = image[y0:y1, x0:x1]
            if upscale and upscale != 1.0:
                tile = cv2.resize(tile, None, fx=upscale, fy=upscale,
                                  interpolation=cv2.INTER_CUBIC)
            tiles.append(tile)
    return tiles
