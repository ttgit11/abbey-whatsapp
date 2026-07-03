"""
models.py — Plain data structures passed between modules.

Kept dependency-free so every other module (and the tests) can import them
cheaply.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class LotDraft:
    """What Abbey produces for one item, before/after staff review."""
    receipt: str = ""
    item_no: str = ""
    sale: str = ""                     # which sale/catalogue this lot belongs to
    category: str = ""
    title: str = ""
    description: str = ""
    low_estimate: Optional[float] = None
    high_estimate: Optional[float] = None
    reserve: Optional[float] = None
    condition_flags: list = field(default_factory=list)
    confidence: float = 0.0            # 0..1, Abbey's own confidence in the ID
    needs_closeup: bool = False
    clarifying_questions: list = field(default_factory=list)  # value-critical Qs to ask staff
    sources: list = field(default_factory=list)   # source names used for pricing
    hero_photo: str = ""               # path to the chosen photo
    candidate_photos: list = field(default_factory=list)
    ai_low_estimate: Optional[float] = None   # what Abbey first said (before staff edits)
    ai_high_estimate: Optional[float] = None
    # Go Auction Appraisal Import fields
    location: str = ""                 # room in the estate (picklist)
    consign_to: str = ""               # sale stream (e.g. Weekly Estate)
    valuer: str = ""
    reserve_rule: str = ""
    go_condition: str = ""             # import "Condition" field (NCV/Donated/Not Taken)
    created: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    @property
    def lot_ref(self) -> str:
        if self.receipt and self.item_no:
            return f"{self.receipt}-{self.item_no}"
        return self.receipt or self.item_no or ""


@dataclass
class LearningProposal:
    """A change Abbey wants to make to how she prices or which sources she trusts.

    `requires_passcode` is decided by security.classify_change().
    """
    kind: str                          # "price_shift" | "source_demote" | "source_promote"
    subject: str                       # category name or source name
    detail: str                        # human-readable explanation
    current_value: float               # e.g. current factor 1.00 or trust score
    proposed_value: float              # e.g. new factor 0.82
    samples: int = 0
    requires_passcode: bool = True
