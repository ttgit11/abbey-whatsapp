"""
memory.py — Abbey's knowledge matrix and correlation engine.

Two things live here:

1. INSIGHTS MATRIX. A weighted store of things Abbey knows beyond raw comps:
     * "note"  — a staff observation captured live at the camera (highest trust,
                 because a person is looking at the item);
     * "rule"  — a teaching heuristic ("DVDs in lots of 50-80 fetch more per unit;
                 100+ lowers the per-unit value");
     * "flag"  — a trend Abbey noticed herself (see correlations).
   Each carries a WEIGHT and a SOURCE, and staff knowledge is weighted above
   historical sold-data so a person's judgement wins.

2. CORRELATIONS. Every saved lot drops an observation (category, title, and how
   far staff moved the price from Abbey's first read). When a pattern is strong
   enough — e.g. three grand pianos all coming in low — Abbey raises a flag so it
   can be surfaced and spoken.

All logic is pure SQLite and unit-tested.
"""

from __future__ import annotations

import sqlite3
import statistics
from datetime import datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS insights (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    subject  TEXT,                 -- category / maker / keyword, lowercased
    kind     TEXT,                 -- 'note' | 'rule' | 'flag'
    text     TEXT,
    weight   REAL NOT NULL DEFAULT 1.0,
    source   TEXT,                 -- 'staff_live' | 'staff_rule' | 'auto' | 'sold_data'
    created  TEXT
);
CREATE TABLE IF NOT EXISTS observations (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT,
    title    TEXT,
    qty      INTEGER DEFAULT 1,
    ai_mid   REAL,
    corr_mid REAL,
    ratio    REAL,                 -- corr_mid / ai_mid  (<1 = came in low)
    ts       TEXT
);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def init_memory(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


# ---------------------------------------------------------------------------
# Insights matrix
# ---------------------------------------------------------------------------
def add_insight(conn, subject: str, kind: str, text: str, *, weight: float,
                source: str) -> int:
    cur = conn.execute(
        "INSERT INTO insights(subject, kind, text, weight, source, created) "
        "VALUES(?,?,?,?,?,?)",
        (subject.strip().lower(), kind, text.strip(), float(weight), source, _now()))
    conn.commit()
    return int(cur.lastrowid)


def all_insights(conn, kinds: tuple[str, ...] | None = None) -> list[dict]:
    q = "SELECT id, subject, kind, text, weight, source, created FROM insights"
    args: tuple = ()
    if kinds:
        q += " WHERE kind IN (%s)" % ",".join("?" * len(kinds))
        args = kinds
    q += " ORDER BY weight DESC, id DESC"
    return [dict(id=r[0], subject=r[1], kind=r[2], text=r[3], weight=r[4],
                 source=r[5], created=r[6]) for r in conn.execute(q, args).fetchall()]


def delete_insight(conn, insight_id: int) -> None:
    conn.execute("DELETE FROM insights WHERE id=?", (insight_id,))
    conn.commit()


def _terms_from(category: str, title: str = "") -> list[str]:
    terms = []
    if category:
        terms.append(category.strip().lower())
    for w in (title or "").lower().split():
        w = "".join(ch for ch in w if ch.isalnum())
        if len(w) >= 4:
            terms.append(w)
    return terms


def context_for(conn, category: str = "", title: str = "", *, max_items: int = 8) -> str:
    """A weighted block of relevant notes/rules/flags to inject into a prompt.

    If no category/title is given, returns the top global rules + flags (so Abbey
    always knows the house rules and current trends). Staff knowledge sorts first.
    """
    rows = all_insights(conn)
    if not rows:
        return ""
    terms = _terms_from(category, title)
    picked = []
    for r in rows:
        subj = r["subject"]
        if not terms:
            if r["kind"] in ("rule", "flag"):
                picked.append(r)
        else:
            if any(subj and (subj in t or t in subj) for t in terms) or r["kind"] == "flag":
                picked.append(r)
        if len(picked) >= max_items:
            break
    if not picked:
        return ""
    label = {"note": "staff note (trusted)", "rule": "house rule", "flag": "trend"}
    lines = [f"  - [{label.get(r['kind'], r['kind'])}] {r['text']}" for r in picked]
    return ("HOUSE KNOWLEDGE — weight these heavily (staff notes outrank guides):\n"
            + "\n".join(lines))


def watch_terms(conn) -> list[str]:
    """Subjects staff have taught Abbey about — used to scan for trends."""
    rows = conn.execute(
        "SELECT DISTINCT subject FROM insights WHERE kind IN ('note','rule') "
        "AND subject <> ''").fetchall()
    return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# Observations + correlations
# ---------------------------------------------------------------------------
def record_observation(conn, category: str, title: str, ai_mid, corr_mid,
                       qty: int = 1) -> None:
    ratio = None
    if ai_mid and corr_mid and ai_mid > 0:
        ratio = corr_mid / ai_mid
    conn.execute(
        "INSERT INTO observations(category, title, qty, ai_mid, corr_mid, ratio, ts) "
        "VALUES(?,?,?,?,?,?,?)",
        (category.strip().lower(), title.strip(), int(qty or 1),
         ai_mid, corr_mid, ratio, _now()))
    conn.commit()


def _direction(median_ratio: float, threshold: float) -> str | None:
    if median_ratio <= 1 - threshold:
        return "low"
    if median_ratio >= 1 + threshold:
        return "high"
    return None


def _flag_text(subject: str, direction: str, n: int, median_ratio: float) -> str:
    pct = abs(1 - median_ratio) * 100
    if direction == "low":
        return (f"{subject} are coming in LOW — {n} recent lots priced about "
                f"{pct:.0f}% under Abbey's first read. Lean lower.")
    return (f"{subject} are running HIGH — {n} recent lots beat Abbey's first read "
            f"by about {pct:.0f}%. She can be a touch bolder.")


def category_flags(conn, *, min_n: int, threshold: float, lookback: int) -> list[dict]:
    rows = conn.execute(
        "SELECT category, ratio FROM observations WHERE ratio IS NOT NULL "
        "ORDER BY id DESC LIMIT ?", (lookback,)).fetchall()
    by_cat: dict[str, list[float]] = {}
    for cat, ratio in rows:
        by_cat.setdefault(cat, []).append(ratio)
    flags = []
    for cat, ratios in by_cat.items():
        if len(ratios) < min_n:
            continue
        med = statistics.median(ratios)
        d = _direction(med, threshold)
        if d:
            flags.append({"subject": cat, "direction": d, "n": len(ratios),
                          "median_ratio": round(med, 3),
                          "text": _flag_text(cat, d, len(ratios), med)})
    return flags


def term_flags(conn, terms: list[str], *, min_n: int, threshold: float,
               lookback: int) -> list[dict]:
    flags = []
    for term in terms:
        term = (term or "").strip().lower()
        if not term:
            continue
        rows = conn.execute(
            "SELECT ratio FROM observations WHERE ratio IS NOT NULL AND "
            "(lower(title) LIKE ? OR category = ?) ORDER BY id DESC LIMIT ?",
            (f"%{term}%", term, lookback)).fetchall()
        ratios = [r[0] for r in rows]
        if len(ratios) < min_n:
            continue
        med = statistics.median(ratios)
        d = _direction(med, threshold)
        if d:
            flags.append({"subject": term, "direction": d, "n": len(ratios),
                          "median_ratio": round(med, 3),
                          "text": _flag_text(term, d, len(ratios), med)})
    return flags


def refresh_flags(conn, *, min_n: int, threshold: float, lookback: int,
                  flag_weight: float = 2.0) -> list[dict]:
    """Recompute correlations and store them as 'flag' insights (replacing old
    auto flags). Returns the current flags."""
    # scan both categories and the specific things staff have taught about
    flags = category_flags(conn, min_n=min_n, threshold=threshold, lookback=lookback)
    seen = {f["subject"] for f in flags}
    for f in term_flags(conn, watch_terms(conn), min_n=min_n, threshold=threshold,
                        lookback=lookback):
        if f["subject"] not in seen:
            flags.append(f)
            seen.add(f["subject"])
    conn.execute("DELETE FROM insights WHERE kind='flag' AND source='auto'")
    for f in flags:
        add_insight(conn, f["subject"], "flag", f["text"],
                    weight=flag_weight, source="auto")
    conn.commit()
    return flags
