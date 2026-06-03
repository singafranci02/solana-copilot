"""Backfill team_clusters.funding_source + wallets.funding_source (F4a).

ALL existing team_clusters have funding_source=NULL because build_team_cluster_post_grad
never resolved it — so funder_reputation has never populated. This walks each cluster's
member tx history via extract_funding_source and fills it in.

LIMITATION: get_transactions_for_address returns only the recent ~100 txs per wallet. For
wallets that have been active since the original funding, the funding tx has scrolled out
of the window → resolves None. Best-effort; most reliable on recently-graduated tokens.

Run on Mac mini:
    uv run python scripts/backfill_funding_source.py
"""

import asyncio
import json
import logging
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.common.db import get_connection
from src.ingest.rpc import RpcClient, extract_funding_source_rpc

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)

MAX_MEMBERS = 10
SLEEP_BETWEEN = 0.3


async def resolve(conn, rpc, token_mint: str, members: list[str]) -> str | None:
    funders: list[str] = []
    for addr in members[:MAX_MEMBERS]:
        try:
            funder = await extract_funding_source_rpc(rpc, addr)
        except Exception:
            continue
        if funder:
            conn.execute(
                """INSERT INTO wallets (address, funding_source) VALUES (?, ?)
                   ON CONFLICT(address) DO UPDATE SET
                       funding_source = COALESCE(wallets.funding_source, excluded.funding_source)""",
                (addr, funder),
            )
            if funder != "cex":
                funders.append(funder)
    conn.commit()
    return Counter(funders).most_common(1)[0][0] if funders else None


async def main() -> None:
    conn = get_connection()
    rows = conn.execute(
        """SELECT token_mint, member_addresses FROM team_clusters
           WHERE funding_source IS NULL AND member_addresses != '[]'""",
    ).fetchall()

    total = len(rows)
    if total == 0:
        logger.info("no team_clusters with NULL funding_source")
        conn.close()
        return

    logger.info("resolving funding_source for %d clusters...", total)
    resolved = 0

    async with RpcClient() as rpc:
        for i, row in enumerate(rows):
            members = json.loads(row["member_addresses"] or "[]")
            if not members:
                continue
            funder = await resolve(conn, rpc, row["token_mint"], members)
            if funder:
                conn.execute(
                    "UPDATE team_clusters SET funding_source = ? WHERE token_mint = ?",
                    (funder, row["token_mint"]),
                )
                conn.commit()
                resolved += 1
                logger.info("%s.. → funder %s..", row["token_mint"][:8], funder[:8])
            if i > 0 and i % 20 == 0:
                logger.info("progress: %d/%d  resolved=%d", i, total, resolved)
            await asyncio.sleep(SLEEP_BETWEEN)

    logger.info("done — resolved %d of %d clusters", resolved, total)
    conn.close()


if __name__ == "__main__":
    asyncio.run(main())
