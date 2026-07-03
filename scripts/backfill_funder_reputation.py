"""Backfill funder_reputation from existing team_clusters + coin_outcomes.

wallet_stats requires token_buyers (which historical REST-poll tokens don't have).
funder_reputation only needs team_clusters + classified outcomes — both exist.

Run on Mac mini:
    uv run python scripts/backfill_funder_reputation.py
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import logging
from src.common.db import get_connection
from src.analyzer.smart_money import update_funder_reputation

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)


def main() -> None:
    conn = get_connection()

    rows = conn.execute(
        """SELECT
               tc.funding_source,
               tc.token_mint,
               tc.supply_pct_at_graduation  AS bundle_pct,
               co.classified
           FROM team_clusters tc
           JOIN coin_outcomes co
             ON co.token_mint = tc.token_mint
            AND co.check_offset_h = 4      -- one outcome per token, not 1h+4h+24h
           JOIN graduation_events ge
             ON ge.token_mint = tc.token_mint
            AND ge.pipeline_version >= 2   -- pre-v2 clusters contain pool addresses
           WHERE tc.funding_source IS NOT NULL
             AND tc.funding_source != 'cex'
             AND co.classified IS NOT NULL
           ORDER BY tc.funding_source""",
    ).fetchall()

    if not rows:
        logger.warning("no data to backfill — run backfill_outcomes.py first")
        conn.close()
        return

    logger.info("backfilling funder_reputation from %d classified graduations...", len(rows))

    for row in rows:
        update_funder_reputation(
            funding_source=row["funding_source"],
            token_mint=row["token_mint"],
            outcome=row["classified"],
            bundle_pct=float(row["bundle_pct"] or 0),
            dev_pct=0.0,
            conn=conn,
        )

    # Report results
    total = conn.execute("SELECT COUNT(*) FROM funder_reputation").fetchone()[0]
    ruggers = conn.execute("SELECT COUNT(*) FROM funder_reputation WHERE is_known_rugger = 1").fetchone()[0]
    multi = conn.execute("SELECT COUNT(*) FROM funder_reputation WHERE rug_count + moon_count + ok_count >= 3").fetchone()[0]

    logger.info("done — %d funders in DB, %d known ruggers, %d with 3+ launches", total, ruggers, multi)

    conn.close()


if __name__ == "__main__":
    main()
