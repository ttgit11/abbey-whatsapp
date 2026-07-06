"""
batch.py — Conversational photo-batch cataloguing (the "go through it with me" flow).

Different from the in-person valuer flow (offsite.py). Here the operator dumps a pile
of photos with no dividers, then Abbey proposes a lot list (one photo = one lot to
start), and the operator refines it in plain English before finalising:

    merge 3 and 4      → photos 3 & 4 become one lot (same item, multiple angles)
    split 2            → undo a merge, back to separate photos
    group 6 7 8        → bundle several photos into one lot (like a pack)
    drop 5             → remove that photo from the catalogue
    done               → finalise: build the list + Go Auction Excel

Free-form notes still work ("3 is chipped", "make 2 cheaper") and attach to that lot.

This module is pure logic (no network, no Claude calls) so it is fully unit-tested.
The server wires photos in and Abbey's analysis + Excel out.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field


DONE_WORDS = {"done", "finished", "finish", "complete", "that's it", "thats it"}
REVIEW_WORDS = {"review", "list", "propose", "group them", "catalogue", "catalog"}


@dataclass
class Photo:
    ref: str                       # local path / media id
    dropped: bool = False


@dataclass
class Lot:
    """A proposed lot = one or more photos treated as a single auction item."""
    photo_idx: list[int] = field(default_factory=list)   # indices into Batch.photos
    note: str = ""
    title: str = ""
    description: str = ""
    category: str = ""
    low: float | None = None
    high: float | None = None
    dirty: bool = True


@dataclass
class Batch:
    receipt: str = ""
    photos: list[Photo] = field(default_factory=list)
    lots: list[Lot] = field(default_factory=list)
    reviewed: bool = False          # has Abbey proposed a list yet?
    finalised: bool = False
    last_activity: float = field(default_factory=time.time)

    def add_photo(self, ref: str) -> None:
        self.photos.append(Photo(ref=ref))
        self.last_activity = time.time()

    def default_lots(self) -> None:
        """One photo = one lot (skipping dropped photos). Called at first review."""
        self.lots = [Lot(photo_idx=[i]) for i, p in enumerate(self.photos) if not p.dropped]

    def live_lots(self) -> list[Lot]:
        return [l for l in self.lots if l.photo_idx]


# --- command parsing -----------------------------------------------------------
_NUMS = re.compile(r"\d+")


def _nums(text: str) -> list[int]:
    return [int(n) for n in _NUMS.findall(text or "")]


def is_done(text: str) -> bool:
    return (text or "").strip().lower() in DONE_WORDS


def wants_review(text: str) -> bool:
    t = (text or "").strip().lower()
    return t in REVIEW_WORDS or t.startswith("review") or t.startswith("list")


def looks_like_receipt(text: str) -> str | None:
    m = re.match(r"^\s*(?:receipt\s*)?#?\s*(\d{3,7})\s*$", text or "", re.I)
    return m.group(1) if m else None


def parse_command(text: str) -> dict:
    """Interpret a refinement command against the CURRENT lot numbering (1-based).
    Returns {'cmd': merge|split|group|drop|note|none, ...}."""
    t = (text or "").strip().lower()
    nums = _nums(t)
    if t.startswith("merge") and len(nums) >= 2:
        return {"cmd": "merge", "lots": nums}
    if t.startswith("group") and len(nums) >= 2:
        return {"cmd": "group", "lots": nums}
    if t.startswith("split") and nums:
        return {"cmd": "split", "lot": nums[0]}
    if t.startswith("drop") and nums:
        return {"cmd": "drop", "lots": nums}
    # "N is chipped", "make N cheaper", "N: <note>" → note on that lot
    if nums:
        return {"cmd": "note", "lot": nums[0], "text": text.strip()}
    return {"cmd": "none"}


# --- applying commands to the lot list -----------------------------------------
def apply_command(batch: Batch, cmd: dict) -> str:
    """Mutate batch.lots per the command. Returns a short human confirmation."""
    lots = batch.live_lots()
    n = len(lots)

    def valid(i: int) -> bool:
        return 1 <= i <= n

    kind = cmd.get("cmd")
    if kind == "merge" or kind == "group":
        targets = sorted(set(cmd["lots"]))
        if not all(valid(i) for i in targets):
            return f"Those numbers are out of range (there are {n} lots)."
        keep = lots[targets[0] - 1]
        for i in targets[1:]:
            src = lots[i - 1]
            keep.photo_idx += src.photo_idx
            if src.note:
                keep.note = (keep.note + " " + src.note).strip()
            src.photo_idx = []          # emptied → drops out of live_lots
        keep.dirty = True
        verb = "Grouped" if kind == "group" else "Merged"
        return f"{verb} lots {', '.join(map(str, targets))} into one."
    if kind == "split":
        i = cmd["lot"]
        if not valid(i):
            return f"Lot {i} is out of range (there are {n} lots)."
        lot = lots[i - 1]
        if len(lot.photo_idx) <= 1:
            return f"Lot {i} is already a single photo."
        first, *rest = lot.photo_idx
        lot.photo_idx = [first]; lot.dirty = True
        for idx in rest:
            batch.lots.append(Lot(photo_idx=[idx]))
        return f"Split lot {i} back into separate photos."
    if kind == "drop":
        targets = sorted(set(cmd["lots"]), reverse=True)
        if not all(valid(i) for i in targets):
            return f"Those numbers are out of range (there are {n} lots)."
        for i in targets:
            for idx in lots[i - 1].photo_idx:
                batch.photos[idx].dropped = True
            lots[i - 1].photo_idx = []
        return f"Dropped lot(s) {', '.join(map(str, sorted(set(cmd['lots']))))}."
    if kind == "note":
        i = cmd["lot"]
        if not valid(i):
            return f"Lot {i} is out of range (there are {n} lots)."
        lots[i - 1].note = (lots[i - 1].note + " " + cmd["text"]).strip()
        lots[i - 1].dirty = True
        return f"Noted on lot {i}."
    return "I didn't catch a command there."


# --- session -------------------------------------------------------------------
class BatchSession:
    def __init__(self):
        self.batch: Batch | None = None

    def on_photo(self, ref: str) -> None:
        if self.batch is None:
            self.batch = Batch()
        if self.batch.finalised:
            self.batch.finalised = False
        self.batch.add_photo(ref)

    def on_text(self, text: str) -> dict:
        text = (text or "").strip()
        rcpt = looks_like_receipt(text)
        if rcpt is not None:
            self.batch = Batch(receipt=rcpt)
            return {"action": "receipt", "receipt": rcpt}
        if self.batch is None:
            return {"action": "noop"}
        self.batch.last_activity = time.time()

        if is_done(text):
            self.batch.finalised = True
            return {"action": "done"}
        if wants_review(text):
            if not self.batch.reviewed:
                self.batch.default_lots()
                self.batch.reviewed = True
            return {"action": "review"}
        # a refinement command (only meaningful after a review)
        if self.batch.reviewed:
            cmd = parse_command(text)
            if cmd["cmd"] != "none":
                msg = apply_command(self.batch, cmd)
                return {"action": "refine", "message": msg}
        return {"action": "noop"}


def format_proposal(batch: Batch) -> str:
    """The review list Abbey sends: numbered lots with photo counts + titles if known."""
    lots = batch.live_lots()
    head = f"Receipt {batch.receipt or '—'} — {len(lots)} lots proposed " \
           f"(reply e.g. 'merge 3 and 4', 'group 6 7 8', 'split 2', 'drop 5', or 'done'):\n"
    lines = [head]
    for i, lot in enumerate(lots, 1):
        pc = len(lot.photo_idx)
        title = lot.title or "(analysing…)"
        est = f"  ${int(lot.low)}–${int(lot.high)}" if lot.low and lot.high else ""
        lines.append(f"{i}. {title}  [{pc} photo{'s' if pc != 1 else ''}]{est}"
                     + (f"  · {lot.note}" if lot.note else ""))
    return "\n".join(lines)


def format_final(batch: Batch) -> str:
    lots = batch.live_lots()
    lines = [f"Receipt {batch.receipt or '—'} — {len(lots)} lots\n"]
    for i, lot in enumerate(lots, 1):
        est = f"${int(lot.low)}–${int(lot.high)}" if lot.low and lot.high else "—"
        lines.append(f"{i}. {lot.title or '(unidentified)'}\n"
                     f"   {lot.description or ''}\n"
                     f"   Est {est}  ·  {lot.category or ''}".rstrip())
    return "\n".join(lines)
