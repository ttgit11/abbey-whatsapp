"""
converse.py — Talking with Abbey about the item in front of you.

One turn = your spoken words (already transcribed) + the item photo + the current
draft, sent to Claude. Abbey replies with:
  * `say`  — a short, natural sentence or two, which is read ALOUD; and
  * `edits`— optional changes to the draft when you asked her to change something
             ("make it cheaper", "call it walnut not teak", "shorten the title").

`parse_reply` and `apply_edits` are pure and unit-tested. `converse_turn` is the
thin network call.
"""

from __future__ import annotations

import base64

from . import agent
from .models import LotDraft

_EDIT_SCALARS = {"title", "description", "category", "low_estimate",
                 "high_estimate", "reserve"}


def build_system_prompt(house_name: str) -> str:
    return (
        f"You are \"Abbey\", the cataloguing assistant at {house_name}, an Australian "
        "auction house. The operator at the receiving desk is SPEAKING to you about the "
        "item shown in the photo; their words were transcribed and may contain small "
        "errors — interpret sensibly. You can see the item and the current draft entry.\n\n"
        "Reply with ONLY a JSON object:\n"
        "{\n"
        '  "say": "a short, natural spoken reply (1-2 sentences, it will be read aloud)",\n'
        '  "action": "research"  — include ONLY if they asked you to look something up, '
        "find prices, or what it's worth; use \"snapshot\" if they asked to see/capture the "
        "source pages; otherwise omit,\n"
        '  "edits": { optional — include ONLY fields they asked you to change:\n'
        '     "title","description","category","low_estimate","high_estimate","reserve",\n'
        '     "add_condition_flags":["..."] }\n'
        "}\n"
        "If they only asked a question, answer it in 'say' and omit 'edits'. Keep money "
        "in AUD hammer. Be honest about uncertainty rather than guessing."
    )


def _draft_summary(draft: LotDraft) -> str:
    return (f"Current draft — title: {draft.title!r}; category: {draft.category!r}; "
            f"description: {draft.description!r}; estimate: "
            f"${draft.low_estimate or 0:.0f}-${draft.high_estimate or 0:.0f}; "
            f"condition: {', '.join(draft.condition_flags) or 'none noted'}.")


def parse_reply(text: str) -> tuple[str, dict, str]:
    """Return (say, edits, action) from a model reply. Never raises.
    `action` is "" or "research"."""
    try:
        data = agent._extract_json(text)
    except Exception:  # noqa: BLE001
        # If the model just spoke plainly, treat the whole thing as the reply.
        return text.strip(), {}, ""
    say = str(data.get("say", "")).strip()
    edits = data.get("edits") or {}
    if not isinstance(edits, dict):
        edits = {}
    action = str(data.get("action", "")).strip().lower()
    if action not in ("research", "snapshot"):
        action = ""
    return say, edits, action


def apply_edits(draft: LotDraft, edits: dict) -> list[str]:
    """Apply spoken edits to the draft. Returns the list of changed field names."""
    changed: list[str] = []

    def num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    for k in _EDIT_SCALARS:
        if k in edits and edits[k] not in (None, ""):
            if k in ("low_estimate", "high_estimate", "reserve"):
                v = num(edits[k])
                if v is None:
                    continue
            else:
                v = str(edits[k]).strip()
                if k == "category":
                    v = v.lower()
            setattr(draft, k, v)
            changed.append(k)

    flags = edits.get("add_condition_flags")
    if isinstance(flags, str):
        flags = [flags]
    for f in flags or []:
        f = str(f).strip()
        if f and f not in draft.condition_flags:
            draft.condition_flags.append(f)
            if "condition_flags" not in changed:
                changed.append("condition_flags")

    # keep low <= high if both known
    if draft.low_estimate and draft.high_estimate and draft.low_estimate > draft.high_estimate:
        draft.low_estimate, draft.high_estimate = draft.high_estimate, draft.low_estimate

    return changed


def converse_turn(client, image_bytes: bytes, draft: LotDraft, user_text: str, *,
                  house_name: str, model: str, max_tokens: int,
                  history: list | None = None) -> tuple[str, list[str], str]:
    """One spoken turn. Returns (spoken_reply, changed_fields, action). Mutates
    `draft` in place with any edits. `action` is "" or "research"."""
    b64 = base64.b64encode(image_bytes).decode()
    convo = ""
    for who, txt in (history or [])[-6:]:
        convo += f"{who}: {txt}\n"
    content = [
        {"type": "image",
         "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
        {"type": "text",
         "text": f"{_draft_summary(draft)}\n\nRecent exchange:\n{convo}"
                 f"Operator just said: \"{user_text}\"\nReturn only the JSON object."},
    ]
    msg = client.messages.create(
        model=model, max_tokens=max_tokens, system=build_system_prompt(house_name),
        messages=[{"role": "user", "content": content}])
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    say, edits, action = parse_reply(text)
    changed = apply_edits(draft, edits)
    return say, changed, action
