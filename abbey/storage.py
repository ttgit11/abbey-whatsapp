"""
storage.py — Persistence for catalogued lots + CSV export + photo handling.

Wraps the SQLite DB (shared with knowledge.py) and provides:
  * saving a reviewed LotDraft
  * writing chosen photos to disk with tidy names
  * exporting a Go Auction / Bidpath friendly CSV
  * an optional "push photos somewhere" step (local folder / S3 / GDrive)

Tested in tests/test_storage.py using a temp dir + in-memory-style file DB.
"""

from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd

from . import knowledge
from .models import LotDraft

LOTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS lots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_ref      TEXT,
    receipt      TEXT,
    item_no      TEXT,
    sale         TEXT,
    category     TEXT,
    title        TEXT,
    description  TEXT,
    low_est      REAL,
    high_est     REAL,
    reserve      REAL,
    condition    TEXT,
    confidence   REAL,
    sources      TEXT,
    hero_photo   TEXT,
    ai_low       REAL,
    ai_high      REAL,
    created      TEXT
);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.executescript(LOTS_SCHEMA)
    _migrate(conn)
    knowledge.init_db(conn)
    from . import memory
    memory.init_memory(conn)
    from . import devlog
    devlog.init_devlog(conn)
    from . import reliability
    reliability.init_reliability(conn)
    reliability.enable_wal(conn)
    from . import uploads
    uploads.init_uploads(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a DB was first created (safe, idempotent)."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(lots)").fetchall()}
    if "sale" not in have:
        conn.execute("ALTER TABLE lots ADD COLUMN sale TEXT")
        conn.commit()
    for col in ("location", "consign_to", "valuer", "reserve_rule", "go_condition"):
        if col not in have:
            conn.execute(f"ALTER TABLE lots ADD COLUMN {col} TEXT")
    conn.commit()


# ---------------------------------------------------------------------------
# Sale folders (a tidy on-disk file system, one folder per sale)
# ---------------------------------------------------------------------------
def safe_name(name: str) -> str:
    keep = "-_. "
    cleaned = "".join(c for c in (name or "") if c.isalnum() or c in keep).strip()
    return cleaned.replace(" ", "-") or "unsorted"


def sale_paths(sales_root: Path, sale: str) -> dict:
    """Return (and create) the folder set for one sale:
        sales/<sale>/photos  and  sales/<sale>/exports
    """
    root = Path(sales_root) / safe_name(sale)
    photos = root / "photos"
    exports = root / "exports"
    snapshots = root / "snapshots"
    for d in (photos, exports, snapshots):
        d.mkdir(parents=True, exist_ok=True)
    return {"root": root, "photos": photos, "exports": exports, "snapshots": snapshots}


def list_sales(sales_root: Path) -> list[str]:
    root = Path(sales_root)
    if not root.exists():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


# ---------------------------------------------------------------------------
# Photos
# ---------------------------------------------------------------------------
def save_photo(frame, photo_dir: Path, lot_ref: str, suffix: str = "") -> str:
    """Write a BGR frame to disk and return the path. The timestamp includes
    microseconds so a burst (or two lots in the same second) never collide."""
    import cv2  # lazy: only the desk app saves photos; the cloud service never calls this
    photo_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    safe = (lot_ref or "lot").replace("/", "-").replace(" ", "")
    name = f"{safe}_{stamp}{('_' + suffix) if suffix else ''}.jpg"
    path = photo_dir / name
    cv2.imwrite(str(path), frame, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
    return str(path)


# ---------------------------------------------------------------------------
# Lots
# ---------------------------------------------------------------------------
def save_lot(conn: sqlite3.Connection, lot: LotDraft) -> int:
    cur = conn.execute(
        """INSERT INTO lots(lot_ref, receipt, item_no, sale, category, title, description,
                            low_est, high_est, reserve, condition, confidence, sources,
                            hero_photo, ai_low, ai_high, created,
                            location, consign_to, valuer, reserve_rule, go_condition)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (lot.lot_ref, lot.receipt, lot.item_no, lot.sale, lot.category, lot.title,
         lot.description, lot.low_estimate, lot.high_estimate, lot.reserve,
         "; ".join(lot.condition_flags), lot.confidence, ", ".join(lot.sources),
         lot.hero_photo, lot.ai_low_estimate, lot.ai_high_estimate, lot.created,
         lot.location, lot.consign_to, lot.valuer, lot.reserve_rule, lot.go_condition),
    )
    conn.commit()
    return int(cur.lastrowid)


def all_lots(conn: sqlite3.Connection) -> pd.DataFrame:
    return pd.read_sql_query("SELECT * FROM lots ORDER BY id", conn)


# ---------------------------------------------------------------------------
# CSV export — Go Auction "Appraisal Import" template
# ---------------------------------------------------------------------------
def export_csv(conn: sqlite3.Connection, out_path: Path, columns: list[str],
               sale: str | None = None) -> Path:
    """Export lots in the exact column layout of Go Auction's Appraisal Import
    template, so the file imports directly. `columns` is accepted for
    compatibility but the layout is fixed to the template.

    Abbey's descriptive condition flags are folded into the Description (the
    import's Condition field is a disposition picklist, not free text).
    """
    df = all_lots(conn)
    if sale is not None and "sale" in df.columns:
        df = df[df["sale"] == sale].reset_index(drop=True)

    def g(col):
        return df.get(col) if col in df.columns else None

    def col(series, default=""):
        return list(series) if series is not None else [default] * len(df)

    titles = col(g("title"))
    descs = col(g("description"))
    conds = col(g("condition"))          # descriptive flags → into Description
    descriptions = []
    for d, c in zip(descs, conds):
        d = (d or "")
        if c:
            d = (d + f" Condition: {c}.").strip()
        descriptions.append(d)

    def money(series):
        out = []
        for v in (list(series) if series is not None else [None] * len(df)):
            if v is None or (isinstance(v, float) and pd.isna(v)):
                out.append("")
            elif isinstance(v, float) and v.is_integer():
                out.append(int(v))
            else:
                out.append(v)
        return out

    out = pd.DataFrame({
        "Line Number": list(range(1, len(df) + 1)),
        "Title": titles,
        "Description": descriptions,
        "Low Estimate": money(g("low_est")),
        "High Estimate": money(g("high_est")),
        "Categories": col(g("category")),
        "Location": col(g("location")),
        "Consign To": col(g("consign_to")),
        "Valuer": col(g("valuer")),
        "Reserve Rule": col(g("reserve_rule")),
        "Reserve Price": money(g("reserve")),
        "Condition": col(g("go_condition")),
    })
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(out_path, index=False)
    return out_path


# ---------------------------------------------------------------------------
# Optional upload
# ---------------------------------------------------------------------------
def push_photos(paths: list[str], mode: str, *, local_folder: str = "",
                s3_bucket: str = "") -> str:
    """Return a human-readable result string. Cloud modes are stubs you enable in
    SETUP_GUIDE.md once credentials are configured."""
    if mode == "none":
        return "Upload disabled (photos kept locally)."
    if mode == "local_folder":
        if not local_folder:
            return "No local folder configured."
        dest = Path(local_folder)
        dest.mkdir(parents=True, exist_ok=True)
        for p in paths:
            try:
                shutil.copy2(p, dest / Path(p).name)
            except OSError as e:
                return f"Copy failed: {e}"
        return f"Copied {len(paths)} photo(s) to {dest}."
    if mode == "s3":
        try:
            import boto3  # noqa: F401  (only needed if S3 mode is used)
        except ImportError:
            return "boto3 not installed — run: pip install boto3"
        # import boto3; s3 = boto3.client('s3')
        # for p in paths: s3.upload_file(p, s3_bucket, Path(p).name)
        return "S3 upload stub — fill in credentials in SETUP_GUIDE.md."
    if mode == "gdrive":
        return "Google Drive stub — see SETUP_GUIDE.md for the Drive setup."
    return f"Unknown upload mode: {mode}"
