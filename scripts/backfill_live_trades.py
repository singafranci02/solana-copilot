"""Backfill full order flow (live_trades) for SURVIVOR tokens over the last N days.

Survivors = graduated tokens whose 4h outcome was moon/ok (not rug). These are
the ~10-20% where strategic push/distribute patterns actually play out, so they're
the highest-value training data and keep Helius credit cost bounded.

Pipeline per token:
  1. resolve the PumpSwap pool via DexScreener pairAddress (graduation_events
     pumpswap_pool_address is often NULL, so DexScreener is primary)
  2. paginate the pool's tx history backward via Helius (`before` cursor) to
     graduation time or the N-day cap
  3. parse_swap each tx (feePayer = trader), tag the wallet, INSERT OR IGNORE

HELIUS CREDIT COST: ~10 credits per 100 txs (one page). Per token = max_pages * 10.
With --max-pages 50 that's up to ~500 credits/token for a very active pool; most
survivors are far smaller. Tune --max-pages against your Helius plan before a full run.

Run on Mac mini:
    uv run python scripts/backfill_live_trades.py              # last 14 days, 50 pages
    uv run python scripts/backfill_live_trades.py --days 7 --max-pages 30
"""

import asyncio
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.common.db import get_connection
from src.ingest.helius import HeliusClient, paginate_address_txs, parse_swap
from src.analyzer.distribution import _fetch_dex_stats
from src.analyzer.wallet_tag import build_mint_context, tag_wallet
from src.analyzer.live_trades import LiveTrade, insert_trades

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)

DAYS = 14
MAX_PAGES = 50
if "--days" in sys.argv:
    DAYS = int(sys.argv[sys.argv.index("--days") + 1])
if "--max-pages" in sys.argv:
    MAX_PAGES = int(sys.argv[sys.argv.index("--max-pages") + 1])


async def backfill_token(helius, conn, mint: str, graduated_at: int) -> int:
    liq, _price, pool = await _fetch_dex_stats(mint)
    if not pool:
        logger.debug("skip %s.. — no pool resolvable", mint[:8])
        return 0

    until_ts = max(graduated_at, int(time.time()) - DAYS * 86_400)
    raw = await paginate_address_txs(
        helius, pool, until_ts=until_ts, max_pages=MAX_PAGES, tx_type="SWAP",
    )
    if not raw:
        return 0

    ctx = build_mint_context(conn, mint)
    trades: list[LiveTrade] = []
    for tx in raw:
        sw = parse_swap(tx)
        if sw is None or sw.token_mint != mint:
            continue
        trades.append(LiveTrade(
            token_mint=mint,
            wallet_address=sw.signer,
            side=sw.side,
            sol_amount=sw.sol_amount,
            token_amount=sw.token_amount,
            ts=sw.timestamp,
            slot=sw.slot,
            signature=tx.get("signature"),
            wallet_tag=tag_wallet(ctx, sw.signer),
            source="backfill",
        ))

    n = insert_trades(conn, trades)
    if n:
        logger.info("%s.. — %d trades backfilled (pool %s..)", mint[:8], n, pool[:8])
    return n


async def main() -> None:
    conn = get_connection()
    cutoff = int(time.time()) - DAYS * 86_400

    rows = conn.execute(
        """SELECT ge.token_mint, ge.graduated_at
           FROM graduation_events ge
           JOIN coin_outcomes co ON co.token_mint = ge.token_mint
                                AND co.check_offset_h = 4
           WHERE ge.graduated_at >= ?
             AND co.classified IN ('moon','ok')
           ORDER BY ge.graduated_at DESC""",
        (cutoff,),
    ).fetchall()
    conn.close()

    total = len(rows)
    if total == 0:
        logger.info("no survivor tokens in the last %dd to backfill", DAYS)
        return

    logger.info("backfilling order flow for %d survivor tokens (last %dd, max %d pages)...",
                total, DAYS, MAX_PAGES)
    grand_total = 0

    async with HeliusClient() as helius:
        for i, row in enumerate(rows):
            conn = get_connection()
            try:
                grand_total += await backfill_token(
                    helius, conn, row["token_mint"], int(row["graduated_at"]),
                )
            except Exception as exc:
                logger.debug("backfill failed for %s..: %s", row["token_mint"][:8], exc)
            finally:
                conn.close()
            if i > 0 and i % 10 == 0:
                logger.info("progress: %d/%d  trades=%d", i, total, grand_total)
            await asyncio.sleep(0.3)

    logger.info("done — %d trades across %d tokens", grand_total, total)


if __name__ == "__main__":
    asyncio.run(main())
