"""Rebuild wallet_graph from clean (pipeline_version >= 2) team clusters.

Run after the v2 learning-table reset if you want to re-seed the graph from
clusters recorded since the fix, instead of waiting for live accumulation.
Outcome-aware: rug outcomes increment rug_co_appearances, mirroring the live
4h path in distribution._update_funder_reputation_from_distribution.

Usage: uv run python scripts/rebuild_wallet_graph.py
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import json
import logging

from src.common.db import get_connection
from src.analyzer.team_memory import update_wallet_graph

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)


def main() -> None:
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT tc.member_addresses, co.classified
               FROM team_clusters tc
               JOIN graduation_events ge
                 ON ge.token_mint = tc.token_mint AND ge.pipeline_version >= 2
               LEFT JOIN coin_outcomes co
                 ON co.token_mint = tc.token_mint AND co.check_offset_h = 4
               ORDER BY ge.graduated_at""",
        ).fetchall()

        if not rows:
            logger.info("no pipeline_version>=2 clusters yet — nothing to rebuild")
            return

        for row in rows:
            members = json.loads(row["member_addresses"] or "[]")
            update_wallet_graph(members, outcome=row["classified"], conn=conn)

        pairs = conn.execute("SELECT COUNT(*) FROM wallet_graph").fetchone()[0]
        logger.info("rebuilt from %d clusters — %d pairs in wallet_graph", len(rows), pairs)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
