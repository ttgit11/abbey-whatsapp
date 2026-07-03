"""
devlog.py — "Abbey's updates": a log Abbey keeps for herself and her admin.

Abbey proactively records things worth a future improvement pass:
  * "error"      — a coding error she hit (so nothing gets silently swallowed);
  * "suggestion" — an idea to work better ("this photo would read better if I looked
                   at it in higher detail / tiled it and zoomed in");
  * "note"       — anything the admin jots down.

Ty (admin) reviews these on the Learning screen, exports them to Markdown, and brings
that back to Claude for an update sweep. Entries are de-duplicated (the same error
seen ten times is one row with a count), and everything persists in the database.

Pure SQLite; fully unit-tested.
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS dev_updates (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    sig      TEXT UNIQUE,
    kind     TEXT,                 -- 'error' | 'suggestion' | 'note'
    area     TEXT,                 -- 'vision' | 'pricing' | 'voice' | ...
    text     TEXT,
    detail   TEXT,
    status   TEXT DEFAULT 'open',  -- 'open' | 'resolved'
    count    INTEGER DEFAULT 1,
    first_ts TEXT,
    last_ts  TEXT
);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def init_devlog(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def _sig(kind: str, area: str, text: str) -> str:
    return hashlib.sha1(f"{kind}|{area}|{text}".encode("utf-8")).hexdigest()[:16]


def log(conn, kind: str, area: str, text: str, detail: str = "") -> int:
    """Record an update. Identical (kind/area/text) entries are merged, bumping a
    count and refreshing the timestamp rather than piling up duplicates."""
    sig = _sig(kind, area, text.strip())
    row = conn.execute("SELECT id, count FROM dev_updates WHERE sig=?", (sig,)).fetchone()
    if row:
        conn.execute(
            "UPDATE dev_updates SET count=count+1, last_ts=?, status='open', detail=? "
            "WHERE id=?", (_now(), detail or "", row[0]))
        conn.commit()
        return int(row[0])
    cur = conn.execute(
        "INSERT INTO dev_updates(sig, kind, area, text, detail, status, count, first_ts, last_ts) "
        "VALUES(?,?,?,?,?,'open',1,?,?)",
        (sig, kind, area, text.strip(), detail or "", _now(), _now()))
    conn.commit()
    return int(cur.lastrowid)


def log_error(conn, area: str, text: str, detail: str = "") -> int:
    return log(conn, "error", area, text, detail)


def log_suggestion(conn, area: str, text: str, detail: str = "") -> int:
    return log(conn, "suggestion", area, text, detail)


def all_updates(conn, status: str | None = None) -> list[dict]:
    q = ("SELECT id, kind, area, text, detail, status, count, first_ts, last_ts "
         "FROM dev_updates")
    args: tuple = ()
    if status:
        q += " WHERE status=?"
        args = (status,)
    # open first, then most-recent
    q += " ORDER BY (status='open') DESC, last_ts DESC"
    return [dict(id=r[0], kind=r[1], area=r[2], text=r[3], detail=r[4], status=r[5],
                 count=r[6], first_ts=r[7], last_ts=r[8])
            for r in conn.execute(q, args).fetchall()]


def set_status(conn, update_id: int, status: str) -> None:
    conn.execute("UPDATE dev_updates SET status=? WHERE id=?", (status, update_id))
    conn.commit()


def delete(conn, update_id: int) -> None:
    conn.execute("DELETE FROM dev_updates WHERE id=?", (update_id,))
    conn.commit()


def counts(conn) -> dict:
    open_rows = all_updates(conn, status="open")
    return {"open": len(open_rows),
            "errors": sum(1 for r in open_rows if r["kind"] == "error"),
            "suggestions": sum(1 for r in open_rows if r["kind"] == "suggestion")}


def export_markdown(conn, house_name: str = "Abbeys") -> str:
    """A Markdown brief the admin can paste back to Claude for an update sweep."""
    rows = all_updates(conn, status="open")
    lines = [f"# {house_name} — Abbey's update log",
             f"_Generated {_now()}. {len(rows)} open items._", ""]
    for kind in ("error", "suggestion", "note"):
        group = [r for r in rows if r["kind"] == kind]
        if not group:
            continue
        lines.append(f"## {kind.title()}s")
        for r in group:
            seen = f" (seen {r['count']}×)" if r["count"] > 1 else ""
            lines.append(f"- **[{r['area']}]** {r['text']}{seen}")
            if r["detail"]:
                lines.append(f"  - detail: {r['detail']}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"
