"""
increments.py — Snap estimates onto real auction bid points, and cap silly spreads.

Auctions don't take arbitrary numbers — bids climb a ladder that grows with price
(the universal 0–2–5–8 / ~10% pattern): …100, 120, 150, 180, 200, 220, 250, 280,
300, 350, 400, 450, 500, 600, 700, 800, 900, 1000, 1200… Estimates should land on
those points, and a low/high pair should sit a rung or two apart — not $200–$1600.

Tuned to Abbeys' low end: $10 steps under $100, then the standard ladder above.
Everything here is pure and unit-tested.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right


def _ladder(cap: int = 200_000) -> list[int]:
    """Build the bid-point ladder up to `cap`.

    Under $100: $10 steps (Abbeys opens cheap goods at $20, $30, $40…).
    $100 and up: the standard 1–1.2–1.5–1.8–2–2.5–3–3.5–4–5–6–7–8–9 × 10^n pattern,
    which is how auction increments actually read across the sheet.
    """
    pts = list(range(10, 100, 10))                      # 10,20,…,90
    mult = [100, 120, 150, 180, 200, 250, 300, 350, 400,
            500, 600, 700, 800, 900]
    decade = 1
    while True:
        for m in mult:
            v = m * decade
            if v < 100:
                continue
            if v > cap:
                return pts
            pts.append(v)
        decade *= 10


LADDER = _ladder()


def snap(value: float, *, up: bool = False) -> int:
    """Snap a dollar value to the nearest bid point (or the next one up if up=True)."""
    if value <= 0:
        return 0
    if value >= LADDER[-1]:
        return LADDER[-1]
    if up:
        i = bisect_left(LADDER, value)
        return LADDER[min(i, len(LADDER) - 1)]
    i = bisect_right(LADDER, value)
    lo = LADDER[i - 1] if i > 0 else LADDER[0]
    hi = LADDER[i] if i < len(LADDER) else LADDER[-1]
    return lo if (value - lo) <= (hi - lo) / 2 else hi


def steps_between(low: int, high: int) -> int:
    """How many ladder rungs separate two snapped values."""
    if low <= 0 or high <= low:
        return 0
    return max(0, bisect_left(LADDER, high) - bisect_left(LADDER, low))


def snap_estimate(low: float, high: float, *, max_steps: int = 3) -> tuple[int, int]:
    """Snap a low/high estimate onto the ladder and cap the spread.

    - Both ends land on real bid points.
    - high is pulled down to at most `max_steps` rungs above low (kills $200–$1600).
    - Guarantees high is at least one rung above low so it's a real range.
    """
    lo = snap(max(0.0, low))
    hi = snap(max(0.0, high))
    if lo <= 0:
        lo = LADDER[0]
    if hi < lo:
        lo, hi = hi, lo
    i_lo = bisect_left(LADDER, lo)
    # cap the top
    if steps_between(lo, hi) > max_steps:
        hi = LADDER[min(i_lo + max_steps, len(LADDER) - 1)]
    # ensure at least one rung of range
    if hi <= lo:
        hi = LADDER[min(i_lo + 1, len(LADDER) - 1)]
    return lo, hi
