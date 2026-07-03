"""
matrix.py — The value & trend matrix.

Builds a graph of the CONCEPTS Abbeys actually deals in — materials, styles,
makers, origins, and categories — mined from the house's own lot titles,
descriptions and categories. Nodes are sized by the value of the lots they touch
and coloured by market heat; edges join concepts that appear together in the same
lot. Each node is tagged with the sale tier it shows up in most (Weekly / Classics
/ a Special sale like stamps, wine, vinyl).

Hard rule: EXTERNAL research may only adjust the heat of concepts that ALREADY
exist here (mined from Abbeys' catalogues). It can never add a node. If a trend
names something Abbeys doesn't sell, it's ignored.

Everything here is pure and unit-tested; the live web-heat refresh runs on the
desk machine and feeds `apply_external_heat`.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

# Concept vocabulary Abbeys actually deals in. Grouped so the graph can show
# families. (Extend freely — mining only keeps terms that actually appear.)
VOCAB: dict[str, list[str]] = {
    "material": ["teak", "oak", "walnut", "mahogany", "pine", "rosewood", "blackwood",
                 "brass", "copper", "bronze", "silver", "sterling", "pewter", "chrome",
                 "glass", "crystal", "porcelain", "ceramic", "pottery", "stoneware",
                 "leather", "cane", "rattan", "wicker", "bakelite", "fibreglass",
                 "marble", "timber", "vinyl"],
    "style": ["mid-century", "midcentury", "retro", "art deco", "deco", "art nouveau",
              "nouveau", "victorian", "edwardian", "georgian", "vintage", "antique",
              "industrial", "modernist", "brutalist", "regency", "colonial"],
    "maker": ["parker", "featherston", "fler", "g-plan", "gplan", "chiswell", "tessa",
              "noblett", "narvik", "royal doulton", "doulton", "wedgwood", "noritake",
              "royal albert", "royal worcester", "villeroy", "waterford", "orrefors",
              "murano", "carlton", "shelley", "minton", "moorcroft", "lalique",
              "georg jensen", "wmf", "poole"],
    "origin": ["danish", "scandinavian", "swedish", "norwegian", "japanese", "chinese",
               "italian", "english", "french", "german", "australian", "american",
               "persian", "belgian"],
}

# Which terms are near-synonyms (fold together so we don't get twin blobs).
_ALIAS = {"midcentury": "mid-century", "art deco": "deco", "gplan": "g-plan",
          "doulton": "royal doulton", "art nouveau": "nouveau"}


def _canon(term: str) -> str:
    return _ALIAS.get(term, term)


@dataclass
class Node:
    name: str
    group: str                       # material | style | maker | origin | category
    count: int = 0                   # how many lots touch it
    value_sum: float = 0.0           # sum of hammer/estimate for those lots
    tier_counts: dict = field(default_factory=lambda: defaultdict(int))
    heat: float = 0.5                # 0 cold … 1 hot (starts neutral)

    @property
    def avg_value(self) -> float:
        return (self.value_sum / self.count) if self.count else 0.0

    @property
    def tier(self) -> str:
        if not self.tier_counts:
            return "Weekly Estate"
        return max(self.tier_counts.items(), key=lambda kv: kv[1])[0]


def _terms_in(text: str) -> set[str]:
    t = f" {text.lower()} "
    found = set()
    for group, words in VOCAB.items():
        for w in words:
            # word-ish boundary match so 'oak' doesn't hit 'cloak'
            if re.search(rf"(?<![a-z]){re.escape(w)}(?![a-z])", t):
                found.add(_canon(w))
    return found


def _group_of(term: str) -> str:
    for group, words in VOCAB.items():
        if term in words or term == _canon(term) and any(_canon(x) == term for x in words):
            return group
    return "concept"


def build_graph(lots: list[dict]) -> tuple[list[Node], list[dict]]:
    """lots: [{title, description, category, value, tier}]. Returns (nodes, edges).

    A node is created for every concept that appears (plus each category). Edges
    count how often two concepts co-occur in the same lot. Value and tier accrue
    to every concept a lot touches.
    """
    nodes: dict[str, Node] = {}
    edge_w: dict[tuple[str, str], int] = defaultdict(int)

    def ensure(name: str, group: str) -> Node:
        if name not in nodes:
            nodes[name] = Node(name=name, group=group)
        return nodes[name]

    for lot in lots:
        text = f"{lot.get('title','')} {lot.get('description','')}"
        concepts = _terms_in(text)
        cat = (lot.get("category") or "").strip()
        present: list[tuple[str, str]] = [(_canon(c), _group_of(_canon(c))) for c in concepts]
        if cat:
            present.append((cat, "category"))
        value = float(lot.get("value") or 0)
        tier = lot.get("tier") or "Weekly Estate"
        seen = set()
        for name, group in present:
            if name in seen:
                continue
            seen.add(name)
            nd = ensure(name, group)
            nd.count += 1
            nd.value_sum += value
            nd.tier_counts[tier] += 1
        names = sorted(seen)
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                edge_w[(names[i], names[j])] += 1

    node_list = sorted(nodes.values(), key=lambda n: -n.count)
    edges = [{"source": a, "target": b, "weight": w}
             for (a, b), w in edge_w.items() if w >= 1]
    return node_list, edges


def apply_external_heat(nodes: list[Node], trends: dict[str, float]) -> int:
    """Nudge node heat from external research. `trends` maps concept -> heat 0..1.
    ONLY concepts that already exist as nodes are touched; unknown trend terms are
    ignored (they can't create nodes). Returns how many nodes were updated."""
    index = {n.name.lower(): n for n in nodes}
    updated = 0
    for term, heat in trends.items():
        key = _canon(term.strip().lower())
        node = index.get(key)
        if node is None:                 # not an Abbeys concept → ignore entirely
            continue
        node.heat = max(0.0, min(1.0, float(heat)))
        updated += 1
    return updated


def heat_from_value(nodes: list[Node]) -> None:
    """Seed heat from Abbeys' own data when no external trend is set: higher
    average value ⇒ warmer. External research later overrides per concept."""
    if not nodes:
        return
    vals = sorted(n.avg_value for n in nodes if n.avg_value > 0)
    if not vals:
        return
    lo, hi = vals[0], vals[-1]
    span = (hi - lo) or 1.0
    for n in nodes:
        n.heat = 0.15 + 0.7 * ((n.avg_value - lo) / span) if n.avg_value > 0 else 0.1


def to_graph_json(nodes: list[Node], edges: list[dict], *, min_count: int = 1) -> dict:
    """Shape for the 3D force-graph front end: sized by count, coloured by heat,
    tiered by ring colour."""
    keep = {n.name for n in nodes if n.count >= min_count}
    gnodes = [{
        "id": n.name, "group": n.group, "count": n.count,
        "avg_value": round(n.avg_value, 2), "heat": round(n.heat, 3),
        "tier": n.tier,
    } for n in nodes if n.name in keep]
    gedges = [e for e in edges if e["source"] in keep and e["target"] in keep]
    return {"nodes": gnodes, "links": gedges}
