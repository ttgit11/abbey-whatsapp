"""
research.py — "Show me information" for a lot.

Runs a REAL web search (via Claude's server-side web_search tool) for the item in
front of the camera and returns structured, source-backed market information:
comparable prices, a short market read, identifying notes, and a suggested hammer
range for an Australian estate auction.

Why this shape:
  * Every comp carries its **source name and URL**, so what's on screen is
    checkable — not numbers the model invented. Those real sources are what feed
    Abbey's source-trust learning (previously it learned on hallucinated names).
  * `parse_research` is pure and unit-tested; the network call `research_lot` is
    thin and only runs on a machine with internet + ANTHROPIC_API_KEY.

Live web results can't be produced in the build sandbox, so the search itself is
verified on the desk machine; the parsing/grounding logic is fully tested here.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field

from . import agent


@dataclass
class Comp:
    title: str = ""
    price_text: str = ""       # as found, e.g. "$120"
    price_value: float | None = None
    source: str = ""           # e.g. "Leonard Joel" / "eBay sold"
    url: str = ""
    date: str = ""             # when it sold, if known


@dataclass
class ResearchResult:
    market_summary: str = ""
    identifying_notes: str = ""
    comps: list = field(default_factory=list)          # list[Comp]
    suggested_low: float | None = None
    suggested_high: float | None = None
    scarce: bool = False       # True when few/no real comps were found

    def source_domains(self) -> list[str]:
        out = []
        for c in self.comps:
            d = _domain(c.url) or (c.source or "").strip().lower()
            if d and d not in out:
                out.append(d)
        return out


def _domain(url: str) -> str:
    m = re.search(r"https?://([^/]+)/?", url or "")
    return (m.group(1).lower().replace("www.", "") if m else "")


def _price_value(text: str) -> float | None:
    if not text:
        return None
    m = re.search(r"([\d][\d,]*)(?:\.\d+)?", str(text).replace(" ", ""))
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


SYSTEM = (
    "You are Abbey, a cataloguer at an Australian auction house. Use web search to "
    "find what the item in the photo actually sells for, favouring AUSTRALIAN results "
    "and recent SOLD prices (auction results, eBay sold, dealer listings). If good "
    "comps are scarce, say so honestly rather than padding. "
    "Return ONLY a JSON object:\n"
    "{\n"
    '  "market_summary": "2-3 sentences on what these fetch and why",\n'
    '  "identifying_notes": "how to tell it apart / what raises or lowers value",\n'
    '  "scarce": true|false,\n'
    '  "comps": [{"title": "...", "price": "$120", "source": "Leonard Joel",\n'
    '             "url": "https://...", "date": "2024"}],\n'
    '  "suggested_low": number, "suggested_high": number\n'
    "}\n"
    "Every comp MUST include a real url you actually found. Prices in AUD."
)


def parse_research(text: str) -> ResearchResult:
    """Turn the model's JSON reply into a ResearchResult. Never raises."""
    try:
        data = agent._extract_json(text)
    except Exception:  # noqa: BLE001
        return ResearchResult(market_summary=text.strip()[:500], scarce=True)

    def num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    comps = []
    raw = data.get("comps")
    if isinstance(raw, list):
        for c in raw:
            if not isinstance(c, dict):
                continue
            pt = str(c.get("price", "")).strip()
            comps.append(Comp(
                title=str(c.get("title", "")).strip(),
                price_text=pt, price_value=_price_value(pt),
                source=str(c.get("source", "")).strip(),
                url=str(c.get("url", "")).strip(),
                date=str(c.get("date", "")).strip(),
            ))

    return ResearchResult(
        market_summary=str(data.get("market_summary", "")).strip(),
        identifying_notes=str(data.get("identifying_notes", "")).strip(),
        comps=comps,
        suggested_low=num(data.get("suggested_low")),
        suggested_high=num(data.get("suggested_high")),
        scarce=bool(data.get("scarce", not comps)),
    )


def spoken_summary(res: ResearchResult) -> str:
    """A short line Abbey can say aloud after researching."""
    n = len(res.comps)
    if n == 0:
        return "I couldn't find solid comparable sales — the market for this looks thin."
    vals = [c.price_value for c in res.comps if c.price_value]
    if vals:
        lo, hi = int(min(vals)), int(max(vals))
        return f"I found {n} comparable {'sale' if n == 1 else 'sales'}, roughly {lo} to {hi} dollars. They're on screen."
    return f"I found {n} comparable listings — they're on screen."


def research_lot(client, image_bytes: bytes, *, model: str, max_tokens: int,
                 title: str = "", category: str = "") -> ResearchResult:
    """Run a real web search for the item and return structured market info."""
    b64 = base64.b64encode(image_bytes).decode()
    content = [
        {"type": "image",
         "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
        {"type": "text",
         "text": f"Item: {title or 'see photo'} (category: {category or 'unknown'}). "
                 "Search the web and return the JSON object only."},
    ]
    msg = client.messages.create(
        model=model, max_tokens=max_tokens, system=SYSTEM,
        messages=[{"role": "user", "content": content}],
        tools=[{"type": "web_search_20250305", "name": "web_search"}])
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    return parse_research(text)
