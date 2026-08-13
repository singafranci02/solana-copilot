"""Classify every coin's team-exit attribution: observed / held / uncertain.

Pure recompute from stored data, no API calls. Re-runnable.

    uv run python scripts/rebuild_attribution.py
"""

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.analyzer.attribution import upsert_attribution
from src.common.db import get_connection


def main() -> None:
    conn = get_connection()
    mints = [r[0] for r in conn.execute(
        """SELECT ct.token_mint FROM coin_trajectory ct
           JOIN tokens t ON t.mint = ct.token_mint
           WHERE t.platform = 'pump.fun' AND ct.n_price_points >= 30""")]
    print(f"classifying {len(mints)} coins")
    tally = Counter()
    for i, m in enumerate(mints, 1):
        tally[upsert_attribution(conn, m)] += 1
        if i % 200 == 0:
            conn.commit()
            print(f"  {i}/{len(mints)}")
    conn.commit()
    total = sum(tally.values())
    for k, v in tally.most_common():
        print(f"  {k:<10} {v:5}  ({v/max(total,1):.1%})")
    conn.close()


if __name__ == "__main__":
    main()
