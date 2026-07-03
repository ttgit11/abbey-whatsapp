"""
offsite.py — The brain of the WhatsApp valuer flow (pure logic, fully testable).

Protocol (as agreed):
  1. First message of a session = the RECEIPT number (text). Then wait for photos.
  2. Photos accumulate into the CURRENT item. A TEXT closes the current item and
     starts the next. The text is optional info that influences the item
     (keywords, damage, a rough measurement, or a price to use as the estimate).
     A text with no photos yet just moves on (empty boundary).
  3. Item numbers auto-increment from 1; a message like "item 5" overrides the number.
  4. "done" / "finished" — or 2 minutes of silence — finalises the job: build the
     list (item · title · description · estimate) and the Go Auction Excel.
  5. After finalising the gate stays open: free-form edits ("make item 3 cheaper",
     "item 5 has a chip") re-run those items and resend the list + Excel.
  6. A new RECEIPT number starts a fresh job.

This module decides WHAT each message means and keeps the running job. The network
glue (Twilio in/out, media download, Claude calls) lives in whatsapp_server.py and
is thin. Everything here is deterministic and unit-tested.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field


DONE_WORDS = {"done", "finished", "finish", "complete", "that's it", "thats it", "end"}
IDLE_FINALISE_SECONDS = 120  # 2 minutes of silence also finalises


@dataclass
class Item:
    number: int
    photos: list[str] = field(default_factory=list)   # local paths / media ids
    note: str = ""                                     # the free-text influence
    # Filled in once Abbey processes it:
    title: str = ""
    description: str = ""
    category: str = ""
    low: float | None = None
    high: float | None = None
    processed: bool = False
    dirty: bool = True                                 # needs (re)processing


@dataclass
class Job:
    receipt: str = ""
    items: list[Item] = field(default_factory=list)
    finalised: bool = False
    last_activity: float = field(default_factory=time.time)

    def current(self) -> Item | None:
        return self.items[-1] if self.items else None

    def open_item(self, number: int | None = None) -> Item:
        taken = {it.number for it in self.items}
        if number is not None and number in taken:
            # override collides with an existing item — fall back to next free number
            number = max(taken) + 1
        num = number if number is not None else (len(self.items) + 1)
        while num in taken:
            num += 1
        it = Item(number=num)
        self.items.append(it)
        return it

    def item_by_number(self, n: int) -> Item | None:
        return next((it for it in self.items if it.number == n), None)


_RECEIPT_RE = re.compile(r"^\s*(?:receipt\s*)?#?\s*(\d{3,7})\s*$", re.I)
_ITEMNO_RE = re.compile(r"\bitem\s*#?\s*(\d{1,4})\b", re.I)
_PRICE_RE = re.compile(r"\$?\s*(\d{2,6})")


def looks_like_receipt(text: str) -> str | None:
    """A bare number (optionally 'receipt 2612') → the receipt id, else None."""
    m = _RECEIPT_RE.match(text or "")
    return m.group(1) if m else None


def is_done(text: str) -> bool:
    return (text or "").strip().lower() in DONE_WORDS


def parse_item_override(text: str) -> int | None:
    m = _ITEMNO_RE.search(text or "")
    return int(m.group(1)) if m else None


def wants_price(text: str) -> float | None:
    """If the note names a price the valuer wants as the estimate, return it.
    Looks for a number attached to $ or a price word — NOT a model/serial number."""
    t = (text or "").lower()
    # $-prefixed amount wins
    m = re.search(r"\$\s*(\d{2,6})(?:\.\d+)?\b", t)
    if m:
        return float(m.group(1))
    # a number right after a price word (estimate/put it at/value/worth/price/reserve)
    m = re.search(r"(?:estimate|reserve|value|worth|price|put(?: it)?(?: at)?)\D{0,12}(\d{2,6})\b", t)
    if m:
        return float(m.group(1))
    return None


class Session:
    """Holds the current job for one WhatsApp sender and interprets each message."""

    def __init__(self):
        self.job: Job | None = None

    # ---- inbound message handlers -------------------------------------------------
    def on_photo(self, media_ref: str) -> str:
        """A photo arrived. Ensure a job + current item exists, attach it."""
        now = time.time()
        if self.job is None:
            # photos before any receipt — start an unlabelled job so nothing is lost
            self.job = Job(receipt="")
        self.job.last_activity = now
        if self.job.finalised:
            # a new burst after finalising, with no new receipt → reopen same job
            self.job.finalised = False
        if not self.job.items:
            self.job.open_item()
        self.job.current().photos.append(media_ref)
        self.job.current().dirty = True
        return "ack"

    def on_text(self, text: str) -> dict:
        """Interpret a text. Returns an action dict for the server to act on:
        {'action': 'receipt'|'next'|'done'|'edit'|'noop', ...}"""
        now = time.time()
        text = (text or "").strip()

        # 1) New receipt → start a fresh job (also the very first message).
        rcpt = looks_like_receipt(text)
        if rcpt is not None:
            self.job = Job(receipt=rcpt, last_activity=now)
            return {"action": "receipt", "receipt": rcpt}

        if self.job is None:
            # stray text before any receipt/photo — ignore politely
            return {"action": "noop", "reason": "no active job yet"}

        self.job.last_activity = now

        # 2) Done → finalise.
        if is_done(text):
            self.job.finalised = True
            return {"action": "done"}

        # 3) After finalising, free-form text = an EDIT instruction.
        if self.job.finalised:
            n = parse_item_override(text)
            existing = {it.number for it in self.job.items if it.photos}
            if n is not None and n not in existing:
                # they named an item that doesn't exist — tell them, change nothing
                return {"action": "edit_miss", "requested": n, "existing": sorted(existing)}
            targets = [n] if n else None
            # mark targeted (or all) items dirty for reprocessing
            for it in self.job.items:
                if targets is None or it.number in targets:
                    it.note = (it.note + " " + text).strip()
                    it.dirty = True
                    it.processed = False
            self.job.finalised = False
            return {"action": "edit", "targets": targets, "text": text}

        # 4) Normal flow: a text closes the current item (attaching the note) and
        #    opens the next. If there's no current item with photos, it's just a
        #    boundary/no-op.
        cur = self.job.current()
        override = parse_item_override(text)
        if cur is None or not cur.photos:
            # empty boundary — if an item number override is present, set next number
            if override is not None:
                self.job.open_item(override)
            return {"action": "next", "empty": True}

        # attach the note to the just-finished item
        cur.note = (cur.note + " " + text).strip() if cur.note else text
        cur.dirty = True
        finished_no = cur.number
        # open the next item (respect an explicit item-number override)
        self.job.open_item(override if override is not None else finished_no + 1)
        return {"action": "next", "finished_item": finished_no}

    # ---- finalisation helpers -----------------------------------------------------
    def idle_finalise_due(self) -> bool:
        return (self.job is not None and not self.job.finalised
                and bool(self.job.items)
                and (time.time() - self.job.last_activity) >= IDLE_FINALISE_SECONDS)

    def items_to_process(self) -> list[Item]:
        if self.job is None:
            return []
        return [it for it in self.job.items if it.dirty and it.photos]


def format_list(job: Job) -> str:
    """The final text list: item · title · description · estimate."""
    n_items = len([i for i in job.items if i.photos])
    lines = [f"Receipt {job.receipt or '—'} — {n_items} item{'s' if n_items != 1 else ''}\n"]
    for it in job.items:
        if not it.photos:
            continue
        est = (f"${int(it.low)}–${int(it.high)}" if it.low and it.high else "—")
        lines.append(f"{it.number}. {it.title or '(unidentified)'}\n"
                     f"   {it.description or ''}\n"
                     f"   Est {est}  ·  {it.category or ''}".rstrip())
    return "\n".join(lines)


def split_for_whatsapp(text: str, limit: int = 1500) -> list[str]:
    """WhatsApp caps ~1600 chars; split on item boundaries, never mid-line."""
    if len(text) <= limit:
        return [text]
    chunks, buf = [], ""
    for line in text.split("\n"):
        if len(buf) + len(line) + 1 > limit and buf:
            chunks.append(buf.rstrip())
            buf = ""
        buf += line + "\n"
    if buf.strip():
        chunks.append(buf.rstrip())
    return chunks
