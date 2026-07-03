"""
reliability.py — Keeping a busy desk safe: never lose a lot, never lose the moat.

Four concerns, all testable:
  1. Retry transient failures (network blips, rate limits, 5xx) with backoff, so a
     momentary hiccup doesn't surface as an error to staff.
  2. Crash-safe database (SQLite WAL) so a power cut mid-write doesn't corrupt the
     learned price bands.
  3. Nightly database backups (kept N deep) so the moat survives disk trouble.
  4. Capture recovery: every capture is recorded the moment the photos hit disk, so
     if the app or machine dies mid-lot, the item is waiting to be resumed, not lost.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import time
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# 1. Retry with backoff
# ---------------------------------------------------------------------------
_TRANSIENT_HINTS = ("timeout", "timed out", "connection", "temporarily", "overloaded",
                    "rate limit", "too many requests", "503", "502", "529", "504",
                    "unavailable", "reset by peer", "econnreset")


def is_transient(exc: Exception) -> bool:
    """True if an exception looks worth retrying (network/rate/5xx), not a real bug."""
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    name = type(exc).__name__.lower()
    if "timeout" in name or "connection" in name:
        return True
    msg = str(exc).lower()
    return any(h in msg for h in _TRANSIENT_HINTS)


def with_retry(fn, *, attempts: int = 3, base_delay: float = 0.5,
               transient=is_transient, sleep=time.sleep):
    """Call fn(); on a transient failure, wait (exponential backoff) and retry up to
    `attempts` times. Non-transient errors raise immediately. Returns fn()'s result."""
    last = None
    for i in range(max(1, attempts)):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            last = e
            if not transient(e) or i == attempts - 1:
                raise
            sleep(base_delay * (2 ** i))
    if last:
        raise last


# ---------------------------------------------------------------------------
# 2. Crash-safe DB
# ---------------------------------------------------------------------------
def enable_wal(conn: sqlite3.Connection) -> str:
    """Turn on write-ahead logging so an interrupted write can't corrupt the file.
    Returns the resulting journal mode."""
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn.execute("PRAGMA journal_mode").fetchone()[0]
    except sqlite3.Error:
        return "unknown"


# ---------------------------------------------------------------------------
# 3. Backups
# ---------------------------------------------------------------------------
def backup_db(db_path: Path, backup_dir: Path, keep: int = 14) -> str:
    """Make a consistent copy of the database (safe even with the app running, via
    the SQLite backup API), then prune to the newest `keep`. Returns the new path."""
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = backup_dir / f"abbey-{stamp}.db"
    src = sqlite3.connect(str(db_path))
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    prune_backups(backup_dir, keep)
    return str(dest)


def list_backups(backup_dir: Path) -> list[Path]:
    backup_dir = Path(backup_dir)
    if not backup_dir.exists():
        return []
    return sorted(backup_dir.glob("abbey-*.db"), key=lambda p: p.stat().st_mtime,
                  reverse=True)


def prune_backups(backup_dir: Path, keep: int) -> int:
    backups = list_backups(backup_dir)
    removed = 0
    for old in backups[max(0, keep):]:
        try:
            old.unlink(); removed += 1
        except OSError:
            pass
    return removed


def latest_backup(backup_dir: Path) -> str | None:
    b = list_backups(backup_dir)
    return str(b[0]) if b else None


def should_backup(backup_dir: Path, min_interval_hours: float = 20.0) -> bool:
    """True if there's no recent-enough backup (so ~nightly cadence is maintained)."""
    b = list_backups(backup_dir)
    if not b:
        return True
    age_h = (time.time() - b[0].stat().st_mtime) / 3600.0
    return age_h >= min_interval_hours


# ---------------------------------------------------------------------------
# 4. Capture recovery
# ---------------------------------------------------------------------------
PENDING_SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_captures (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT,
    receipt TEXT,
    item_no TEXT,
    sale    TEXT,
    photos  TEXT,                  -- JSON list of photo paths
    status  TEXT DEFAULT 'open'    -- 'open' | 'done'
);
"""


def init_reliability(conn: sqlite3.Connection) -> None:
    conn.executescript(PENDING_SCHEMA)
    conn.commit()


def record_capture(conn, receipt: str, item_no: str, sale: str,
                   photos: list[str]) -> int:
    cur = conn.execute(
        "INSERT INTO pending_captures(ts, receipt, item_no, sale, photos, status) "
        "VALUES(?,?,?,?,?,'open')",
        (datetime.now().isoformat(timespec="seconds"), receipt, item_no, sale,
         json.dumps(list(photos))))
    conn.commit()
    return int(cur.lastrowid)


def clear_capture(conn, capture_id: int) -> None:
    conn.execute("UPDATE pending_captures SET status='done' WHERE id=?", (capture_id,))
    conn.commit()


def pending_captures(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT id, ts, receipt, item_no, sale, photos FROM pending_captures "
        "WHERE status='open' ORDER BY id DESC").fetchall()
    out = []
    for r in rows:
        try:
            photos = json.loads(r[5] or "[]")
        except (ValueError, TypeError):
            photos = []
        out.append(dict(id=r[0], ts=r[1], receipt=r[2], item_no=r[3], sale=r[4],
                        photos=photos))
    return out
