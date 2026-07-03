"""
ingest.py — Learn Abbeys' real price bands from uploaded sold-hammer data.

This is the moat. You upload a CSV of past SOLD lots (title/description, category,
and the hammer price). On command Abbey:
  1. parses it (tolerant of different column names),
  2. computes a realistic price band per category from the real hammer prices
     (default the 25th–75th percentile — the honest middle of what things fetch),
  3. proposes updating the house comps, which — once applied — become the primary
     anchor for every future estimate and are **remembered** in the database.

All logic here is pure and unit-tested. Applying the bands writes to the comps
table via knowledge.set_comp, so it persists across restarts.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass

from . import knowledge


@dataclass
class SoldLot:
    title: str = ""
    category: str = ""
    hammer: float | None = None
    date: str = ""


_PRICE_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")

# Header aliases (lowercased, punctuation-stripped, contains-match).
_TITLE_KEYS = ("name", "title", "lot title", "item name", "item")
_TITLE_EXCLUDE = ("company", "shipping", "billing", "first", "last", "bank", "user")
_CATEGORY_KEYS = ("category", "department", "section")
_DATE_KEYS = ("sale date", "sold date", "date")


def _to_price(v) -> float | None:
    if v is None:
        return None
    m = _PRICE_RE.search(str(v).replace(",", ""))
    if not m:
        return None
    try:
        val = float(m.group(0))
    except ValueError:
        return None
    return val if val > 0 else None


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", (s or "").strip().lower())


def _find_col(headers: list[str], keys: tuple[str, ...], exclude=()) -> int:
    norm = [_norm(h) for h in headers]
    for k in keys:                       # exact first
        for i, h in enumerate(norm):
            if h == k and not any(x in h for x in exclude):
                return i
    for k in keys:                       # then contains
        for i, h in enumerate(norm):
            if k in h and not any(x in h for x in exclude):
                return i
    return -1


def _find_price_col(headers: list[str]) -> int:
    """Prefer the HAMMER (sold) price over replacement/start/reserve/estimate."""
    norm = [_norm(h) for h in headers]
    for want in ("hammer", "sold", "realised", "realized", "result"):
        for i, h in enumerate(norm):
            if want in h:
                return i
    bad = ("replacement", "start", "reserve", "low", "high", "cost", "increment",
           "presale", "bid", "est")
    for i, h in enumerate(norm):
        if "price" in h and not any(b in h for b in bad):
            return i
    return -1


def _find_header_row(rows: list[list[str]]) -> int:
    """Find the header row, skipping any title/blank preamble (Go Auction exports
    start with a sale-title line)."""
    for idx, row in enumerate(rows[:15]):
        norm = [_norm(c) for c in row]
        has_cat = any("category" in c for c in norm)
        has_price = any(("hammer" in c) or ("sold" in c) or ("price" in c) for c in norm)
        if has_cat and has_price:
            return idx
    return 0


def parse_sold_csv(text: str) -> list[SoldLot]:
    """Parse a CSV of past sold lots — tolerant of the Go Auction results export
    (a title row on top, and columns like 'Hammer Price[$]' and 'Name')."""
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return []
    h = _find_header_row(rows)
    headers = rows[h]
    ti = _find_col(headers, _TITLE_KEYS, exclude=_TITLE_EXCLUDE)
    ci = _find_col(headers, _CATEGORY_KEYS)
    pi = _find_price_col(headers)
    di = _find_col(headers, _DATE_KEYS)
    out: list[SoldLot] = []
    for r in rows[h + 1:]:
        if not any(str(cell).strip() for cell in r):
            continue

        def cell(idx):
            return r[idx].strip() if 0 <= idx < len(r) else ""

        out.append(SoldLot(
            title=cell(ti), category=cell(ci) or "uncategorised",
            hammer=_to_price(cell(pi)), date=cell(di),
        ))
    return out


def _percentile(sorted_vals: list[float], pct: float) -> float:
    """Linear-interpolation percentile (pct in 0..100)."""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * (pct / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = k - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def learn_bands(sold: list[SoldLot], *, low_pct: float = 25.0, high_pct: float = 75.0,
                min_samples: int = 3) -> dict[str, tuple[float, float, int]]:
    """Compute a (low, high, n) band per category from real hammer prices.

    Categories with fewer than `min_samples` priced lots are skipped (not enough
    to be trustworthy). Bands are rounded to the nearest $10.
    """
    by_cat: dict[str, list[float]] = {}
    for s in sold:
        if s.hammer and s.hammer > 0:
            by_cat.setdefault(s.category.strip().lower(), []).append(s.hammer)
    bands: dict[str, tuple[float, float, int]] = {}
    for cat, vals in by_cat.items():
        if len(vals) < min_samples:
            continue
        vals.sort()
        lo = _percentile(vals, low_pct)
        hi = _percentile(vals, high_pct)
        from . import increments
        lo, hi = increments.snap_estimate(lo, hi, max_steps=4)  # bands can be a touch wider
        bands[cat] = (float(lo), float(hi), len(vals))
    return bands


def propose_updates(conn, bands: dict[str, tuple[float, float, int]]) -> list[dict]:
    """Compare learned bands to current house comps; return a human-readable diff."""
    proposals = []
    for cat, (lo, hi, n) in sorted(bands.items()):
        existing = knowledge.get_comp(conn, cat)
        proposals.append({
            "category": cat, "new_low": lo, "new_high": hi, "samples": n,
            "old_low": existing["low"] if existing else None,
            "old_high": existing["high"] if existing else None,
            "is_new": existing is None,
        })
    return proposals


def apply_bands(conn, bands: dict[str, tuple[float, float, int]],
                source: str = "sold-data") -> int:
    """Write learned bands into the house comps (remembered). Returns count."""
    for cat, (lo, hi, _n) in bands.items():
        knowledge.set_comp(conn, cat, lo, hi, source=source)
    return len(bands)


# Column aliases for a FULL results export (for importing lots, not just bands).
_DESC_KEYS = ("description", "desc", "long description", "catalogue description")
_LOWEST_KEYS = ("low est", "low estimate", "estimate low", "low")
_HIGHEST_KEYS = ("high est", "high estimate", "estimate high", "high")
_HAMMER_KEYS = ("hammer price", "hammer", "sold price", "sale price", "realised", "price")


def _find_col2(header: list[str], keys: tuple[str, ...]) -> int | None:
    norm = [h.strip().lower().strip('"') for h in header]
    for i, h in enumerate(norm):
        if any(k in h for k in keys):
            return i
    return None


def import_lots_csv(conn, csv_text: str, sale: str, *, storage_mod,
                    tier: str = "") -> tuple[int, int]:
    """Import past lots from a results CSV into the lots table so the matrix and
    history can use them. Estimates come from the export's own low/high (snapped);
    if absent, the hammer price seeds a band. Returns (imported, skipped).

    Photos are not imported (historical lots have none) — these are data-only rows.
    """
    import csv as _csv
    import io
    from .models import LotDraft
    from . import increments

    rows = list(_csv.reader(io.StringIO(csv_text)))
    # find the header row (first row containing a title-ish and a category-ish column)
    header_idx = None
    for i, r in enumerate(rows[:10]):
        low = [c.strip().lower() for c in r]
        if any("categor" in c for c in low) and any(
                any(k in c for k in _TITLE_KEYS) for c in low):
            header_idx = i
            break
    if header_idx is None:
        return (0, 0)
    header = rows[header_idx]
    ti = _find_col2(header, _TITLE_KEYS)
    ci = _find_col2(header, _CATEGORY_KEYS)
    di = _find_col2(header, _DESC_KEYS)
    li = _find_col2(header, _LOWEST_KEYS)
    hi = _find_col2(header, _HIGHEST_KEYS)
    hami = _find_col2(header, _HAMMER_KEYS)
    if ti is None or ci is None:
        return (0, 0)

    imported = skipped = 0
    for r in rows[header_idx + 1:]:
        if len(r) <= max(x for x in (ti, ci) if x is not None):
            skipped += 1
            continue
        title = r[ti].strip()
        category = r[ci].strip()
        if not title or not category:
            skipped += 1
            continue
        desc = r[di].strip() if di is not None and len(r) > di else ""
        low = _to_price(r[li]) if li is not None and len(r) > li else None
        high = _to_price(r[hi]) if hi is not None and len(r) > hi else None
        hammer = _to_price(r[hami]) if hami is not None and len(r) > hami else None
        # prefer the export's estimates; else derive a small band around the hammer
        if low and high:
            slo, shi = increments.snap_estimate(low, high, max_steps=4)
        elif hammer:
            slo, shi = increments.snap_estimate(hammer * 0.8, hammer * 1.2, max_steps=4)
        else:
            slo = shi = None
        draft = LotDraft(
            sale=sale, category=category, title=title, description=desc,
            low_estimate=float(slo) if slo else None,
            high_estimate=float(shi) if shi else None,
            consign_to=tier or "Weekly Estate")
        storage_mod.save_lot(conn, draft)
        imported += 1
    return (imported, skipped)
