"""
uploads.py — Getting a sale's photos off the desk machine, reliably.

Two real backends:
  * LocalFolderBackend — copy to a mapped NAS/drive folder (fully working, tested);
  * S3Backend — upload to an S3 bucket via boto3 (real code; the network call needs
    credentials + boto3, so it's verified on the desk machine).

The important part is the QUEUE: photos are queued per sale, uploaded with
retry/backoff, and anything that fails is kept as 'failed' so it can be retried
later — an upload is never silently lost. Queue logic, dedup, status transitions and
the local backend are all unit-tested.
"""

from __future__ import annotations

import shutil
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from . import reliability

QUEUE_SCHEMA = """
CREATE TABLE IF NOT EXISTS upload_queue (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    sale       TEXT,
    path       TEXT,
    remote     TEXT,
    status     TEXT DEFAULT 'pending',   -- 'pending' | 'uploaded' | 'failed'
    attempts   INTEGER DEFAULT 0,
    last_error TEXT,
    ts         TEXT
);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def init_uploads(conn: sqlite3.Connection) -> None:
    conn.executescript(QUEUE_SCHEMA)
    conn.commit()


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------
class LocalFolderBackend:
    def __init__(self, dest: str):
        self.dest = Path(dest) if dest else None

    def available(self) -> tuple[bool, str]:
        if not self.dest:
            return False, "No destination folder set."
        return True, str(self.dest)

    def upload(self, path: str, *, sale: str = "") -> str:
        target = (self.dest / sale) if sale else self.dest
        target.mkdir(parents=True, exist_ok=True)
        out = target / Path(path).name
        shutil.copy2(path, out)
        return str(out)


class S3Backend:
    def __init__(self, bucket: str, prefix: str = "", region: str | None = None):
        self.bucket = bucket
        self.prefix = prefix or ""
        self.region = region

    def available(self) -> tuple[bool, str]:
        try:
            import boto3  # noqa: F401
        except Exception:  # noqa: BLE001
            return False, "boto3 not installed — run: pip install boto3"
        if not self.bucket:
            return False, "No S3 bucket set."
        return True, f"s3://{self.bucket}/{self.prefix}".rstrip("/")

    def key_for(self, path: str, sale: str = "") -> str:
        parts = [p for p in [self.prefix.strip("/"), sale, Path(path).name] if p]
        return "/".join(parts)

    def upload(self, path: str, *, sale: str = "") -> str:
        import boto3
        s3 = boto3.client("s3", region_name=self.region)
        key = self.key_for(path, sale)
        s3.upload_file(path, self.bucket, key)
        return f"s3://{self.bucket}/{key}"


def make_backend(mode: str, *, local_folder: str = "", s3_bucket: str = "",
                 s3_prefix: str = "", s3_region: str | None = None):
    if mode == "local_folder":
        return LocalFolderBackend(local_folder)
    if mode == "s3":
        return S3Backend(s3_bucket, s3_prefix, s3_region)
    return None   # "none" or unknown → uploads disabled


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------
def queue_files(conn, sale: str, paths: list[str]) -> int:
    """Add files to the upload queue for a sale. Skips ones already pending/uploaded;
    re-queues previously failed ones. Returns how many are now pending."""
    added = 0
    for p in paths:
        row = conn.execute("SELECT id, status FROM upload_queue WHERE sale=? AND path=?",
                           (sale, p)).fetchone()
        if row and row[1] in ("pending", "uploaded"):
            continue
        if row and row[1] == "failed":
            conn.execute("UPDATE upload_queue SET status='pending', ts=? WHERE id=?",
                         (_now(), row[0]))
        else:
            conn.execute("INSERT INTO upload_queue(sale, path, status, attempts, ts) "
                         "VALUES(?,?, 'pending', 0, ?)", (sale, p, _now()))
        added += 1
    conn.commit()
    return added


def pending_uploads(conn, sale: str | None = None) -> list[dict]:
    q = "SELECT id, sale, path, attempts FROM upload_queue WHERE status='pending'"
    args: tuple = ()
    if sale:
        q += " AND sale=?"
        args = (sale,)
    q += " ORDER BY id"
    return [dict(id=r[0], sale=r[1], path=r[2], attempts=r[3])
            for r in conn.execute(q, args).fetchall()]


def mark_uploaded(conn, upload_id: int, remote: str) -> None:
    conn.execute("UPDATE upload_queue SET status='uploaded', remote=?, ts=? WHERE id=?",
                 (remote, _now(), upload_id))
    conn.commit()


def mark_failed(conn, upload_id: int, error: str) -> None:
    conn.execute("UPDATE upload_queue SET status='failed', attempts=attempts+1, "
                 "last_error=?, ts=? WHERE id=?", (error[:200], _now(), upload_id))
    conn.commit()


def upload_status(conn, sale: str | None = None) -> dict:
    q = "SELECT status, COUNT(*) FROM upload_queue"
    args: tuple = ()
    if sale:
        q += " WHERE sale=?"
        args = (sale,)
    q += " GROUP BY status"
    counts = {"pending": 0, "uploaded": 0, "failed": 0}
    for status, n in conn.execute(q, args).fetchall():
        counts[status] = n
    return counts


def process_queue(conn, backend, *, sale: str | None = None, max_items: int = 500,
                  retry_attempts: int = 3, base_delay: float = 0.5,
                  sleep=time.sleep) -> dict:
    """Upload everything pending (optionally for one sale). Each file is retried with
    backoff; failures are recorded and left for a later retry. Returns a summary."""
    if backend is None:
        return {"uploaded": 0, "failed": 0, "note": "uploads are off (mode 'none')"}
    ok, detail = backend.available()
    if not ok:
        return {"uploaded": 0, "failed": 0, "error": detail}
    uploaded = failed = 0
    for row in pending_uploads(conn, sale)[:max_items]:
        try:
            remote = reliability.with_retry(
                lambda: backend.upload(row["path"], sale=row["sale"]),
                attempts=retry_attempts, base_delay=base_delay, sleep=sleep)
            mark_uploaded(conn, row["id"], remote)
            uploaded += 1
        except Exception as e:  # noqa: BLE001
            mark_failed(conn, row["id"], str(e))
            failed += 1
    return {"uploaded": uploaded, "failed": failed}


def retry_failed(conn, sale: str | None = None) -> int:
    """Flip failed uploads back to pending so process_queue picks them up."""
    q = "UPDATE upload_queue SET status='pending' WHERE status='failed'"
    args: tuple = ()
    if sale:
        q += " AND sale=?"
        args = (sale,)
    cur = conn.execute(q, args)
    conn.commit()
    return cur.rowcount
