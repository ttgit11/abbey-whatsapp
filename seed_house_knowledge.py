"""
seed_house_knowledge.py — Preload Abbey with Abbeys' own starting comps.

Run once after install:   python seed_house_knowledge.py

These bands are the 25th–75th percentile of REAL hammer prices from sale 2706
(sale no. 109, 617 sold lots), keyed to the Go Auction categories. They are far
better than a guess but come from a single sale — upload more results on the
Learning screen and Abbey refines them (and her corrections fine-tune from there).
"""

import config
from abbey import knowledge, storage

# category (lowercase Go Auction) -> (low, high) hammer AUD, from real 2706 results.
HOUSE_COMPS = {
    'antique implements & practical items'        : (50, 100),
    'artwork & decorative items'                  : (50, 110),
    'asian decorative arts'                       : (50, 80),
    'books & ephemera'                            : (40, 50),
    'ceramics, porcelain & glass'                 : (50, 80),
    'clocks & scientific equipments'              : (60, 160),
    'clothing & accessories'                      : (50, 110),
    'coins & banknotes'                           : (90, 360),
    'collectables, toys, cards & misc'            : (60, 110),
    'electrical - computers, hifi, audio'         : (80, 190),
    'electrical, appliances & lighting'           : (60, 160),
    'furniture - antique'                         : (70, 310),
    'furniture - modern'                          : (80, 210),
    'jewellery & watches'                         : (60, 220),
    'music & multimedia'                          : (60, 90),
    'musical instruments'                         : (60, 80),
    'outdoor, garage & garden'                    : (60, 180),
    'rugs & carpets'                              : (100, 250),
    'silverware and metalware'                    : (60, 130),
    'sporting goods & memorabilia'                : (130, 170),
    'stamps'                                      : (60, 100),
    'tools'                                       : (60, 160),
    'wines & spirits'                             : (150, 300),
}

# Sources Abbey may lean on, pre-marked trusted. She'll learn to demote any that
# lead to estimates you keep correcting.
TRUSTED_SOURCES = [
    "abbeys past sales", "carters price guide", "antiques reporter",
    "leonard joel results", "gibsons results",
]


def seed(conn) -> tuple[int, int]:
    """Seed the house comps + trusted sources into an existing connection.
    Idempotent — set_comp/set_source_trust upsert, so re-running is safe."""
    for cat, (lo, hi) in HOUSE_COMPS.items():
        knowledge.set_comp(conn, cat, lo, hi, source="sold-data (sale 2706)")
    for s in TRUSTED_SOURCES:
        knowledge.set_source_trust(conn, s, trusted=True)
    return len(HOUSE_COMPS), len(TRUSTED_SOURCES)


def main():
    conn = storage.connect(config.DB_PATH)
    n_comps, n_src = seed(conn)
    print(f"Seeded {n_comps} comps and {n_src} trusted sources.")
    print(f"Database: {config.DB_PATH}")


if __name__ == "__main__":
    main()
