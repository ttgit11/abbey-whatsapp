"""
agent.py — The Claude side of Abbey.

Responsibilities:
  * Build a system prompt that puts ABBEYS' OWN comps and trusted sources first,
    then applies the learned price factors, so estimates are anchored to the
    house's real results (and the "antiques are worth a bit less" reality).
  * Send the chosen photo to Claude and get back a strict JSON block.
  * Parse that JSON into a LotDraft, robustly.

`parse_response()` is pure and unit-tested. The network call `analyze_item()` is
thin and only runs on the desk machine with ANTHROPIC_API_KEY set.
"""

from __future__ import annotations

import base64
import json
import re

from .models import LotDraft

# Categories Abbey should classify into (extend freely; drives comp lookup).
import config

# Abbey classifies into the auction house's real Go Auction categories, so her
# output imports directly and her learned bands line up with the sold-data export.
DEFAULT_CATEGORIES = list(config.GO_CATEGORIES)


def build_system_prompt(house_name: str, comps: list[dict], trusted: list[str],
                        buyers_premium_pct: float, insights_block: str = "") -> str:
    comp_lines = "\n".join(
        f"  - {c['category']}: ${c['low']:.0f}–${c['high']:.0f}" for c in comps
    ) or "  (none recorded yet)"
    trusted_lines = ", ".join(trusted) if trusted else "(none flagged yet)"
    knowledge_section = f"\n\n{insights_block}\n" if insights_block else "\n"
    category_list = ", ".join(config.GO_CATEGORIES)

    return f"""You are "Abbey", the cataloguing assistant for {house_name}, an Australian
auction house running weekly estate sales in Melbourne. You look at ONE item photo
and produce a professional catalogue entry.

HARD RULES
- Estimates are in AUD and are HAMMER (pre buyer's premium of {buyers_premium_pct:.0f}%).
- Anchor estimates to the HOUSE COMPS below first. These are {house_name}'s own
  recent results and matter more than any external guide. The Australian brown-goods
  market is soft: when unsure, lean to the LOWER end.
- Titles are trade-standard and concise (one line, no filler).
- Descriptions are ONE tight sentence (about 20–30 words, never more than 40). Lead with
  the object, then material/timber, period/style, and ONE standout feature. Drop anything
  a bidder can see in the photo or that doesn't affect value. No padding, no salesmanship,
  no repeating the title. Condition goes in condition_flags, not the description.
- NEVER end a description with instructions or caveats like "check for a maker's mark",
  "verify the model", "maker not confirmed", or "underside not inspected". A description
  is a finished statement about the item, not a note-to-self. If something is genuinely
  unknown and value-critical, put it in clarifying_questions instead — never in the description.
- If you cannot identify the item confidently from this photo, say so: set
  needs_closeup=true and lower your confidence, rather than guessing.
- CLARIFYING QUESTIONS: if a detail you cannot see would MATERIALLY change the value,
  add it to clarifying_questions — be specific. Examples: an ambiguous maker
  ("Is this Chiswell or Parker? it changes the value"); a fridge/appliance
  ("What's the model number or litre capacity?"); a quantity that affects per-unit
  value. Ask only when it actually matters.
- Prefer these trusted external sources if you reference the market at all: {trusted_lines}.
{knowledge_section}
GO AUCTION CATEGORIES (choose the single best fit, copied verbatim):
{category_list}

HOUSE COMPS ({house_name}):
{comp_lines}

Reply with ONLY a JSON object, no prose, in exactly this shape:
{{
  "category": "copy EXACTLY one of the Go Auction categories listed above",
  "title": "string",
  "description": "ONE concise sentence, ~20-30 words, no filler",
  "low_estimate": number,
  "high_estimate": number,
  "condition_flags": ["short phrases"],
  "needs_closeup": true|false,
  "clarifying_questions": ["specific questions only if value-critical, else empty"],
  "confidence": 0.0-1.0,
  "sources": ["names of any sources you leaned on, else empty"]
}}"""


def _extract_json(text: str) -> dict:
    """Pull the first JSON object out of a model reply, tolerant of stray prose
    or ```json fences."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    # Find the outermost {...}
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("No JSON object found in model reply.")
    return json.loads(text[start:end + 1])


def parse_response(text: str, receipt: str = "", item_no: str = "") -> LotDraft:
    """Turn a raw model reply into a LotDraft. Never raises on missing fields —
    fills sensible defaults and flags low confidence instead."""
    try:
        data = _extract_json(text)
    except (ValueError, json.JSONDecodeError):
        return LotDraft(
            receipt=receipt, item_no=item_no, title="(could not read item)",
            description="Abbey could not parse a result — re-shoot or catalogue by hand.",
            needs_closeup=True, confidence=0.0,
        )

    def num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def as_list(v):
        # Models sometimes return a bare string where a list is expected; without
        # this, iterating the string would store one character per element.
        if v is None:
            return []
        if isinstance(v, str):
            v = [v]
        if not isinstance(v, (list, tuple)):
            return []
        return [str(x) for x in v if str(x).strip()]

    return LotDraft(
        receipt=receipt,
        item_no=item_no,
        category=str(data.get("category", "AA No Category set")).strip(),
        title=str(data.get("title", "")).strip(),
        description=str(data.get("description", "")).strip(),
        low_estimate=num(data.get("low_estimate")),
        high_estimate=num(data.get("high_estimate")),
        condition_flags=as_list(data.get("condition_flags")),
        needs_closeup=bool(data.get("needs_closeup", False)),
        clarifying_questions=as_list(data.get("clarifying_questions")),
        confidence=float(num(data.get("confidence")) or 0.0),
        sources=[s.strip().lower() for s in as_list(data.get("sources"))],
        ai_low_estimate=num(data.get("low_estimate")),
        ai_high_estimate=num(data.get("high_estimate")),
    )


def _parse_string_list(text: str) -> list[str]:
    """Extract a JSON array of strings from a model reply, tolerantly."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return []
    try:
        arr = json.loads(text[start:end + 1])
    except (ValueError, json.JSONDecodeError):
        return []
    return [str(x).strip() for x in arr if str(x).strip()]


def alternative_descriptions(client, image_bytes: bytes, *, model: str, max_tokens: int,
                             title: str, category: str, n: int = 3) -> list[str]:
    """Return up to `n` alternative one-to-two sentence descriptions for the item,
    for when staff aren't happy with the first wording. One cheap call.

    Pure parsing (`_parse_string_list`) is unit-tested; the network call is thin.
    """
    b64 = base64.b64encode(image_bytes).decode()
    system = (
        "You are Abbey, cataloguing for an Australian auction house. Give alternative "
        "catalogue DESCRIPTIONS for the item in the photo — plain, sentence case, one to "
        "two tight sentences each, varying the emphasis (form, material, period, features, "
        "condition). No salesmanship. Reply with ONLY a JSON array of strings.")
    content = [
        {"type": "image",
         "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
        {"type": "text",
         "text": f"Item so far: {title or 'unknown'} (category: {category or 'unknown'}). "
                 f"Give {n} alternative descriptions as a JSON array of strings only."},
    ]
    msg = client.messages.create(model=model, max_tokens=max_tokens, system=system,
                                 messages=[{"role": "user", "content": content}])
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    return _parse_string_list(text)[:n]


def analyze_item(client, image_bytes: bytes, *, model: str, max_tokens: int,
                 system_prompt: str, receipt: str = "", item_no: str = "",
                 enable_web: bool = False) -> LotDraft:
    """Send one photo to Claude and return a parsed LotDraft.

    `client` is an anthropic.Anthropic() instance (created in the app, where the
    API key is available). Kept out of import-time so tests don't need the SDK.
    """
    b64 = base64.b64encode(image_bytes).decode()
    content = [
        {"type": "image",
         "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
        {"type": "text",
         "text": (f"Catalogue this item. Receipt {receipt}, item {item_no}. "
                  "Return only the JSON object.")},
    ]
    kwargs = dict(model=model, max_tokens=max_tokens, system=system_prompt,
                  messages=[{"role": "user", "content": content}])
    if enable_web:
        kwargs["tools"] = [{"type": "web_search_20250305", "name": "web_search"}]

    msg = client.messages.create(**kwargs)
    text = "".join(block.text for block in msg.content if getattr(block, "type", "") == "text")
    return parse_response(text, receipt=receipt, item_no=item_no)


def reanalyze_with_answers(client, image_bytes: bytes, *, model: str, max_tokens: int,
                           system_prompt: str, draft: LotDraft,
                           answers: list[tuple[str, str]], enable_web: bool = False) -> LotDraft:
    """Re-catalogue an item given staff answers to Abbey's clarifying questions.

    Sends the photo, the current draft, and the Q&A back to Claude and asks it to
    REWRITE the whole entry incorporating the new facts — not just append them.
    Returns a fresh LotDraft (preserving receipt/item/sale/photos from the old one).
    """
    b64 = base64.b64encode(image_bytes).decode()
    qa = "\n".join(f"  Q: {q}\n  A: {a}" for q, a in answers)
    brief = (
        "Re-catalogue this item. Here is your CURRENT draft:\n"
        f"  Title: {draft.title}\n"
        f"  Category: {draft.category}\n"
        f"  Description: {draft.description}\n"
        f"  Estimate: {draft.ai_low_estimate}-{draft.ai_high_estimate}\n\n"
        "Staff have answered your questions with new facts:\n"
        f"{qa}\n\n"
        "Rewrite the ENTIRE entry to incorporate these facts naturally — improve the "
        "title and description so they read as a proper catalogue entry that reflects "
        "the new information (keep the description to ONE tight sentence, ~20-30 words; "
        "do NOT just append the answers as loose words). "
        "Re-estimate if the new facts change value. Return ONLY the JSON object."
    )
    content = [
        {"type": "image",
         "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
        {"type": "text", "text": brief},
    ]
    kwargs = dict(model=model, max_tokens=max_tokens, system=system_prompt,
                  messages=[{"role": "user", "content": content}])
    if enable_web:
        kwargs["tools"] = [{"type": "web_search_20250305", "name": "web_search"}]
    msg = client.messages.create(**kwargs)
    text = "".join(block.text for block in msg.content if getattr(block, "type", "") == "text")
    new = parse_response(text, receipt=draft.receipt, item_no=draft.item_no)
    # Preserve identity + photos from the prior draft
    new.sale = draft.sale
    new.hero_photo = draft.hero_photo
    new.candidate_photos = draft.candidate_photos
    return new


# ---------------------------------------------------------------------------
# "Look closer" — detailed multi-tile analysis
# ---------------------------------------------------------------------------
DETAIL_SYSTEM = (
    "You are Abbey, cataloguing at an Australian auction house. You are shown the WHOLE "
    "item photo followed by several ZOOMED-IN tiles of that same photo. Study the tiles "
    "for fine detail the wide shot misses: maker's marks, labels, model/serial numbers, "
    "capacity or dimensions, signatures, hallmarks, stampings, and any damage. "
    "Return ONLY a JSON object:\n"
    "{\n"
    '  "maker": "if a mark/label identifies it, else empty",\n'
    '  "model_number": "if visible, else empty",\n'
    '  "found_marks": ["short notes on marks/labels/signatures you can read"],\n'
    '  "condition_notes": ["fine condition detail visible up close"],\n'
    '  "refined_description": "one tight sentence, ~20-30 words, using the new detail",\n'
    '  "answered": ["any earlier open question this detail resolves"]\n'
    "}\n"
    "Only state what you can actually see. Empty strings/lists where nothing is found."
)


def parse_detail(text: str) -> dict:
    """Parse the detailed-analysis JSON. Never raises; always returns the full shape."""
    try:
        d = _extract_json(text)
    except Exception:  # noqa: BLE001
        return {"maker": "", "model_number": "", "found_marks": [], "condition_notes": [],
                "refined_description": "", "answered": []}

    def as_list(v):
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        if isinstance(v, str) and v.strip():
            return [v.strip()]
        return []

    return {"maker": str(d.get("maker", "")).strip(),
            "model_number": str(d.get("model_number", "")).strip(),
            "found_marks": as_list(d.get("found_marks")),
            "condition_notes": as_list(d.get("condition_notes")),
            "refined_description": str(d.get("refined_description", "")).strip(),
            "answered": as_list(d.get("answered"))}


def analyze_detailed(client, whole_bytes: bytes, tile_bytes: list[bytes], *,
                     model: str, max_tokens: int, title: str = "", category: str = "",
                     open_questions: list | None = None) -> dict:
    """Send the whole image + zoomed tiles and return parsed detail findings."""
    content = [{"type": "text",
                "text": f"Item: {title or 'see photo'} ({category or 'unknown'}). "
                        f"Open questions: {', '.join(open_questions or []) or 'none'}. "
                        "Whole photo first, then zoomed tiles:"},
               {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                             "data": base64.b64encode(whole_bytes).decode()}}]
    for tb in tile_bytes:
        content.append({"type": "image", "source": {"type": "base64",
                        "media_type": "image/jpeg", "data": base64.b64encode(tb).decode()}})
    msg = client.messages.create(model=model, max_tokens=max_tokens, system=DETAIL_SYSTEM,
                                 messages=[{"role": "user", "content": content}])
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    return parse_detail(text)
