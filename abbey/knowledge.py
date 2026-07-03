"""
knowledge.py — Abbey's memory and learning engine.

Three jobs:

  1. HOUSE COMPS. Holds Abbeys' own category price bands (e.g. "balloon-back
     chairs set of 4: 80–250"). These are the *primary* anchor for estimates —
     Abbeys' real hammer history, not generic guide prices. Abbey biases toward
     these before any web number.

  2. PRICE-CORRECTION LEARNING. Every time staff change an estimate, we store the
     ratio (corrected / AI). When enough corrections accumulate for a category
     and they point the same way, Abbey proposes a shift to that category's
     factor. Small shifts (within the auto band) apply automatically; large ones
     become a LearningProposal that needs the passcode. This is how "antiques are
     worth a bit less" gets baked in over time instead of being argued each week.

  3. SOURCE TRUST. Tracks which external sources (LiveAuctioneers, Carter's, a
     particular dealer, etc.) tend to lead to estimates that survived review vs.
     ones that got corrected hard. Repeatedly-bad sources are proposed for
     demotion (passcode-gated).

All state lives in SQLite. Functions take a connection so tests can use an
in-memory DB. Fully unit-tested in tests/test_knowledge.py.
"""

from __future__ import annotations

import sqlite3
import statistics
from datetime import datetime

from .models import LearningProposal

SCHEMA = """
CREATE TABLE IF NOT EXISTS comps (
    category   TEXT PRIMARY KEY,
    low        REAL NOT NULL,
    high       REAL NOT NULL,
    source     TEXT DEFAULT 'house',
    updated    TEXT
);
CREATE TABLE IF NOT EXISTS adjustments (
    category   TEXT PRIMARY KEY,
    factor     REAL NOT NULL DEFAULT 1.0,   -- multiplier applied to fresh AI estimates
    samples    INTEGER NOT NULL DEFAULT 0,
    updated    TEXT
);
CREATE TABLE IF NOT EXISTS corrections (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    category   TEXT,
    ai_mid     REAL,
    corr_mid   REAL,
    ratio      REAL,
    ts         TEXT
);
CREATE TABLE IF NOT EXISTS sources (
    name       TEXT PRIMARY KEY,
    good       INTEGER NOT NULL DEFAULT 0,
    bad        INTEGER NOT NULL DEFAULT 0,
    trusted    INTEGER NOT NULL DEFAULT 1,   -- 1 trusted, 0 demoted
    updated    TEXT
);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _mid(low, high):
    if low is None or high is None:
        return None
    return (float(low) + float(high)) / 2.0


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


# ---------------------------------------------------------------------------
# 1. House comps
# ---------------------------------------------------------------------------
def set_comp(conn, category: str, low: float, high: float, source: str = "house") -> None:
    conn.execute(
        "INSERT INTO comps(category, low, high, source, updated) VALUES(?,?,?,?,?) "
        "ON CONFLICT(category) DO UPDATE SET low=excluded.low, high=excluded.high, "
        "source=excluded.source, updated=excluded.updated",
        (category.strip().lower(), float(low), float(high), source, _now()),
    )
    conn.commit()


def get_comp(conn, category: str):
    row = conn.execute(
        "SELECT low, high, source FROM comps WHERE category=?",
        (category.strip().lower(),),
    ).fetchone()
    if row:
        return {"low": row[0], "high": row[1], "source": row[2]}
    return None


def all_comps(conn) -> list[dict]:
    rows = conn.execute("SELECT category, low, high, source, updated FROM comps ORDER BY category").fetchall()
    return [{"category": r[0], "low": r[1], "high": r[2], "source": r[3], "updated": r[4]} for r in rows]


# ---------------------------------------------------------------------------
# 2. Adjustments (the learned multiplier) + corrections
# ---------------------------------------------------------------------------
def get_factor(conn, category: str) -> float:
    row = conn.execute(
        "SELECT factor FROM adjustments WHERE category=?",
        (category.strip().lower(),),
    ).fetchone()
    return float(row[0]) if row else 1.0


def effective_estimate(conn, category: str, ai_low: float, ai_high: float) -> tuple[float, float]:
    """Apply the learned factor to a fresh AI estimate, then (optionally) reconcile
    with the house comp band by nudging toward it. Returns (low, high) rounded to
    the nearest $10."""
    factor = get_factor(conn, category)
    low = float(ai_low) * factor
    high = float(ai_high) * factor

    comp = get_comp(conn, category)
    if comp:
        # Blend 50/50 toward the house band so our own history has real pull.
        low = 0.5 * low + 0.5 * comp["low"]
        high = 0.5 * high + 0.5 * comp["high"]

    from . import increments
    if low > high:                 # models occasionally swap the bounds
        low, high = high, low
    lo, hi = increments.snap_estimate(low, high, max_steps=3)
    return float(lo), float(hi)


def record_correction(conn, category: str, ai_low, ai_high, corr_low, corr_high) -> None:
    """Log a staff correction as a ratio of corrected-mid to AI-mid."""
    ai_mid, corr_mid = _mid(ai_low, ai_high), _mid(corr_low, corr_high)
    if not ai_mid or not corr_mid or ai_mid <= 0:
        return
    ratio = corr_mid / ai_mid
    conn.execute(
        "INSERT INTO corrections(category, ai_mid, corr_mid, ratio, ts) VALUES(?,?,?,?,?)",
        (category.strip().lower(), ai_mid, corr_mid, ratio, _now()),
    )
    conn.commit()


def _recent_ratios(conn, category: str, lookback: int) -> list[float]:
    rows = conn.execute(
        "SELECT ratio FROM corrections WHERE category=? ORDER BY id DESC LIMIT ?",
        (category.strip().lower(), lookback),
    ).fetchall()
    return [r[0] for r in rows]


def propose_price_shift(conn, category: str, *, min_samples: int, lookback: int,
                        auto_band_pct: float) -> LearningProposal | None:
    """If corrections for a category consistently point away from the current
    factor, propose a new factor. Returns None if not enough evidence.

    The proposal's `requires_passcode` is True when the change exceeds the auto
    band; the caller (app) then demands the passcode before apply_price_shift().
    """
    ratios = _recent_ratios(conn, category, lookback)
    if len(ratios) < min_samples:
        return None

    # Median target factor implied by staff corrections.
    target = statistics.median(ratios)
    current = get_factor(conn, category)
    # How far does the target move us from the current factor?
    change_pct = (target - current) / current * 100.0 if current else 0.0
    if abs(change_pct) < 1.0:
        return None  # negligible

    return LearningProposal(
        kind="price_shift",
        subject=category.strip().lower(),
        detail=(f"{len(ratios)} recent corrections suggest '{category}' estimates "
                f"should move {change_pct:+.0f}% (factor {current:.2f} → {target:.2f})."),
        current_value=current,
        proposed_value=round(target, 3),
        samples=len(ratios),
        requires_passcode=abs(change_pct) > auto_band_pct,
    )


def apply_price_shift(conn, category: str, factor: float, samples: int = 0) -> None:
    conn.execute(
        "INSERT INTO adjustments(category, factor, samples, updated) VALUES(?,?,?,?) "
        "ON CONFLICT(category) DO UPDATE SET factor=excluded.factor, "
        "samples=excluded.samples, updated=excluded.updated",
        (category.strip().lower(), float(factor), int(samples), _now()),
    )
    conn.commit()


def auto_learn(conn, category: str, *, min_samples: int, lookback: int,
               auto_band_pct: float) -> LearningProposal | None:
    """Called after each correction. Applies a *small* shift automatically and
    returns None; for a *large* shift, returns a LearningProposal for approval.
    """
    proposal = propose_price_shift(
        conn, category, min_samples=min_samples, lookback=lookback,
        auto_band_pct=auto_band_pct,
    )
    if proposal is None:
        return None
    if not proposal.requires_passcode:
        apply_price_shift(conn, category, proposal.proposed_value, proposal.samples)
        return None
    return proposal


def explain_estimate(conn, category: str, ai_low, ai_high, shown_low, shown_high,
                     market_low=None, market_high=None) -> str:
    """A short, plain 'why this price' note for staff and training."""
    parts = []
    comp = get_comp(conn, category)
    factor = get_factor(conn, category)
    if ai_low and ai_high:
        parts.append(f"Abbey's first read was ${ai_low:.0f}-${ai_high:.0f}")
    if abs(factor - 1.0) > 0.01:
        parts.append(f"adjusted by your learned factor {factor:.2f}")
    if comp:
        parts.append(f"blended toward the house band ${comp['low']:.0f}-${comp['high']:.0f}")
    if market_low and market_high:
        parts.append(f"market comps sit around ${market_low:.0f}-${market_high:.0f}")
    tail = f" → shown as ${shown_low:.0f}-${shown_high:.0f}." if shown_low and shown_high else "."
    if not parts:
        return f"Estimate shown as ${shown_low or 0:.0f}-${shown_high or 0:.0f}."
    return "; ".join(parts) + tail


# ---------------------------------------------------------------------------
# 3. Source trust
# ---------------------------------------------------------------------------
def _ensure_source(conn, name: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO sources(name, updated) VALUES(?,?)",
        (name.strip().lower(), _now()),
    )


def record_source_outcome(conn, name: str, good: bool) -> None:
    """After a lot is reviewed: did the source's pricing survive (good) or get
    corrected hard (bad)?"""
    _ensure_source(conn, name)
    col = "good" if good else "bad"
    conn.execute(
        f"UPDATE sources SET {col}={col}+1, updated=? WHERE name=?",
        (_now(), name.strip().lower()),
    )
    conn.commit()


def source_trust(conn, name: str) -> float:
    """Bayesian-ish trust score in 0..1 (starts at 0.5 with no data)."""
    row = conn.execute(
        "SELECT good, bad, trusted FROM sources WHERE name=?",
        (name.strip().lower(),),
    ).fetchone()
    if not row:
        return 0.5
    good, bad, trusted = row
    if not trusted:
        return 0.0
    return (good + 1) / (good + bad + 2)


def trusted_sources(conn) -> list[str]:
    rows = conn.execute("SELECT name FROM sources WHERE trusted=1 ORDER BY name").fetchall()
    return [r[0] for r in rows]


def propose_source_demotion(conn, name: str, *, min_uses: int,
                            bad_rate: float) -> LearningProposal | None:
    row = conn.execute(
        "SELECT good, bad, trusted FROM sources WHERE name=?",
        (name.strip().lower(),),
    ).fetchone()
    if not row:
        return None
    good, bad, trusted = row
    uses = good + bad
    if not trusted or uses < min_uses:
        return None
    rate = bad / uses if uses else 0.0
    if rate < bad_rate:
        return None
    return LearningProposal(
        kind="source_demote",
        subject=name.strip().lower(),
        detail=(f"Source '{name}' has led to poor estimates {bad}/{uses} times "
                f"({rate*100:.0f}%). Abbey wants to stop trusting it."),
        current_value=1.0,
        proposed_value=0.0,
        samples=uses,
        requires_passcode=True,   # changing trusted sources is always gated
    )


def set_source_trust(conn, name: str, trusted: bool) -> None:
    _ensure_source(conn, name)
    conn.execute(
        "UPDATE sources SET trusted=?, updated=? WHERE name=?",
        (1 if trusted else 0, _now(), name.strip().lower()),
    )
    conn.commit()


def all_sources(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT name, good, bad, trusted, updated FROM sources ORDER BY name"
    ).fetchall()
    return [
        {"name": r[0], "good": r[1], "bad": r[2], "trusted": bool(r[3]),
         "trust_score": source_trust(conn, r[0]), "updated": r[4]}
        for r in rows
    ]
