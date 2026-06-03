"""Backfill post_grad_swaps for recently graduated tokens.

LIMITATION: Helius get_transactions_for_address returns only the most recent
~100 txs per wallet. For tokens graduated more than ~24-36h ago, the relevant
post-graduation swaps have scrolled out of that window, so backfilled data
would be silently incomplete. This script therefore defaults to tokens
graduated within the last 36 hours.

Run on Mac mini:
    uv run python scripts/backfill_post_grad_swaps.py
    uv run python scripts/backfill_post_grad_swaps.py --hours 24
"""

import asyncio
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.common.db import get_connection
from src.ingest.solana_tracker import SolanaTrackerClient
from src.analyzer.post_grad_swaps import fetch_team_swaps, compute_metrics, upsert_swaps
from src.analyzer.distribution import (
    _fetch_liquidity_usd, _load_grad_positions, _ALIVE_LIQUIDITY_FLOOR,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)

WINDOW_HOURS = 36
if "--hours" in sys.argv:
    WINDOW_HOURS = int(sys.argv[sys.argv.index("--hours") + 1])


async def main() -> None:
    conn = get_connection()
    cutoff = int(time.time()) - WINDOW_HOURS * 3600

    rows = conn.execute(
        """SELECT ge.token_mint, ge.graduated_at,
                  tc.member_addresses, tc.is_bc_sniper
           FROM graduation_events ge
           JOIN team_clusters tc ON tc.token_mint = ge.token_mint
           WHERE ge.graduated_at >= ?
             AND tc.member_addresses IS NOT NULL
           ORDER BY ge.graduated_at DESC""",
        (cutoff,),
    ).fetchall()

    total = len(rows)
    if total == 0:
        logger.info("no tokens graduated in the last %dh to backfill", WINDOW_HOURS)
        conn.close()
        return

    logger.info("backfilling team swaps for %d tokens (last %dh)...", total, WINDOW_HOURS)
    alive = 0
    total_swaps = 0

    for i, row in enumerate(rows):
        mint = row["token_mint"]
        members = json.loads(row["member_addresses"] or "[]")
        is_sniper = bool(row["is_bc_sniper"])
        graduated_at = int(row["graduated_at"])
        if not members:
            continue

        liquidity = await _fetch_liquidity_usd(mint)
        if liquidity is not None and liquidity < _ALIVE_LIQUIDITY_FLOOR:
            logger.debug("skip %s.. — dead (liq=$%.0f)", mint[:8], liquidity)
            continue
        alive += 1

        async with SolanaTrackerClient() as st:
            swaps = await fetch_team_swaps(st, mint, sorted(members), since_ts=graduated_at)

        sniper_set = set(members) if is_sniper else set()
        n = upsert_swaps(conn, mint, swaps, sniper_set, is_team=True)
        total_swaps += n

        grad_positions = _load_grad_positions(mint, conn)
        metrics = compute_metrics(swaps, grad_positions, sniper_set or set(members))

        # Update existing post_grad_behavior rows with the computed aggregates
        conn.execute(
            """UPDATE post_grad_behavior
               SET team_buy_count = ?, team_sell_count = ?, team_net_sol = ?,
                   coordinated_sell_count = ?, snipers_sold_pct = ?, liquidity_usd = ?
               WHERE token_mint = ?""",
            (
                metrics.team_buy_count, metrics.team_sell_count, metrics.team_net_sol,
                metrics.coordinated_sell_count, metrics.snipers_sold_pct, liquidity, mint,
            ),
        )
        conn.commit()

        if swaps:
            logger.info(
                "%s..  %d swaps (buy=%d sell=%d net=%.2f SOL coord=%d)",
                mint[:8], n, metrics.team_buy_count, metrics.team_sell_count,
                metrics.team_net_sol or 0, metrics.coordinated_sell_count,
            )

        if i > 0 and i % 10 == 0:
            logger.info("progress: %d/%d  alive=%d  swaps=%d", i, total, alive, total_swaps)
        await asyncio.sleep(0.3)

    conn.close()
    logger.info("done — %d alive tokens, %d swaps recorded", alive, total_swaps)


if __name__ == "__main__":
    asyncio.run(main())
