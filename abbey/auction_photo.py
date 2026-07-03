"""
auction_photo.py — Assess frames and crop them to auction photography standards.

ONE Claude call per angle (fast): the model returns BOTH the quality assessment
(person? item? focus? clutter?) AND the item's crop bounds. The crop itself is
then computed locally with OpenCV on the full-resolution frame — instant, and
the saved photo keeps full quality while only a downscaled copy goes to the API.

Pure logic (downscaling, crop math, JSON parsing) is unit-tested in selfcheck.
The API call is a thin wrapper verified on the desk machine.
"""

from __future__ import annotations

import base64
import json

import cv2
import numpy as np

# One combined prompt: assessment + crop bounds in a single reply.
COMBINED_PROMPT = """You are Abbey, an expert auction-house photographer checking a live camera frame.

Auction photo standards: one item, centred, sharp, evenly lit, minimal background,
NO people/hands/reflections of people.

Return ONLY a JSON object, no prose:
{
  "has_item": true|false,
  "has_person": true|false,
  "is_focused": true|false,
  "background_clutter": "minimal"|"moderate"|"excessive",
  "lighting_quality": "good"|"fair"|"poor",
  "composition_score": 0.0-1.0,
  "rejection_reason": "short reason, or null if fine",
  "item_x_min": 0.0-1.0, "item_x_max": 0.0-1.0,
  "item_y_min": 0.0-1.0, "item_y_max": 0.0-1.0,
  "target_aspect": "square"|"4:3",
  "padding": 0.05-0.15
}
item_* are the item's bounding box as fractions of image width/height."""


# ---------------------------------------------------------------------------
# Pure helpers (tested)
# ---------------------------------------------------------------------------
def downscale(frame: np.ndarray, max_dim: int = 1024) -> np.ndarray:
    """Shrink so the longest side is <= max_dim (keeps aspect). Never upscales."""
    h, w = frame.shape[:2]
    scale = max_dim / max(h, w)
    if scale >= 1.0:
        return frame
    return cv2.resize(frame, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)


def parse_reply(text: str) -> dict:
    """Parse the model's JSON reply, tolerant of ```json fences and stray prose."""
    text = (text or "").strip()
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1] if len(parts) > 1 else text
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return {}
    try:
        return json.loads(text[start:end + 1])
    except Exception:  # noqa: BLE001
        return {}


def readiness(data: dict, min_score: float = 0.35, reject_clutter: bool = False) -> tuple[bool, str | None]:
    """Decide ready/not from an assessment dict; return (ready, reason).

    Only hard blocks are a person in frame or no item visible. Clutter is allowed
    by default (deceased-estate desks are busy); the score bar is low so Abbey
    accepts usable angles rather than being fussy."""
    if not data:
        return False, "I couldn't read that frame."
    if data.get("has_person"):
        return False, "I can see a person — step out of frame."
    if not data.get("has_item", False):
        return False, "I can't see the item clearly."
    if reject_clutter and data.get("background_clutter") == "excessive":
        return False, "Too much background clutter."
    if float(data.get("composition_score", 0) or 0) < min_score:
        return False, data.get("rejection_reason") or "Give me a slightly clearer angle."
    return True, None


def apply_crop_bounds(frame: np.ndarray, data: dict) -> np.ndarray:
    """Crop the FULL-RES frame using fractional bounds + padding from the model.
    Falls back to the whole frame on nonsense bounds. Pure math — tested."""
    h, w = frame.shape[:2]
    try:
        x0 = float(data.get("item_x_min", 0.0)); x1 = float(data.get("item_x_max", 1.0))
        y0 = float(data.get("item_y_min", 0.0)); y1 = float(data.get("item_y_max", 1.0))
        pad = float(data.get("padding", 0.10))
    except (TypeError, ValueError):
        return frame
    if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
        return frame
    bw, bh = (x1 - x0) * w, (y1 - y0) * h
    x0p = max(0, int(x0 * w - bw * pad)); x1p = min(w, int(x1 * w + bw * pad))
    y0p = max(0, int(y0 * h - bh * pad)); y1p = min(h, int(y1 * h + bh * pad))
    if x1p - x0p < 20 or y1p - y0p < 20:      # degenerate box → keep original
        return frame
    return frame[y0p:y1p, x0p:x1p]


def sharpness(frame: np.ndarray) -> float:
    """Laplacian variance — higher = sharper. Used to pick the hero angle."""
    grey = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
    return float(cv2.Laplacian(grey, cv2.CV_64F).var())


# ---------------------------------------------------------------------------
# The single API call (thin; verified on the desk machine)
# ---------------------------------------------------------------------------
def assess_and_crop(client, frame: np.ndarray, model: str,
                    min_score: float = 0.35, reject_clutter: bool = False
                    ) -> tuple[dict, np.ndarray | None, str | None]:
    """ONE Claude call: assess the (downscaled) frame and get crop bounds.
    Returns (assessment, cropped_full_res_or_None, reject_reason_or_None)."""
    small = downscale(frame, 1024)
    ok, enc = cv2.imencode(".jpg", small, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
    if not ok:
        return {}, None, "Couldn't encode the frame."
    img_b64 = base64.b64encode(enc.tobytes()).decode()

    resp = client.messages.create(
        model=model, max_tokens=400,
        messages=[{"role": "user", "content": [
            {"type": "text", "text": COMBINED_PROMPT},
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": img_b64}},
        ]}],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    data = parse_reply(text)
    ready, reason = readiness(data, min_score=min_score, reject_clutter=reject_clutter)
    if not ready:
        return data, None, reason
    return data, apply_crop_bounds(frame, data), None
