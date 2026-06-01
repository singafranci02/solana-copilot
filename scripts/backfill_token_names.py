"""Backfill symbol/name for tokens stored as UNKNOWN via DexScreener.

Many historical tokens were saved as UNKNOWN/Unknown because the Helius
metadata response was parsed at the wrong nesting level. This re-fetches
their symbol/name from DexScreener and updates both SQLite and Supabase.

Run on Mac mini:
    uv run python scripts/backfill_token_names.py
"""

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.common.db import get_connection
from src.ingest.graduation_monitor import _dexscreener_symbol_name

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)

BATCH_SIZE = 5
SLEEP_BETWEEN = 2.0


async def main() -> None:
    conn = get_connection()
    rows = conn.execute(
        """SELECT mint FROM tokens
           WHERE symbol = 'UNKNOWN' OR symbol IS NULL OR symbol = ''
              OR name = 'Unknown' OR name IS NULL OR name = ''""",
    ).fetchall()
    conn.close()

    mints = [r["mint"] for r in rows]
    total = len(mints)
    if total == 0:
        logger.info("no UNKNOWN tokens to backfill")
        return

    logger.info("backfilling names for %d tokens...", total)
    fixed = 0

    from src.common import supabase_sync as sb

    for i in range(0, total, BATCH_SIZE):
        batch = mints[i : i + BATCH_SIZE]
        results = await asyncio.gather(*[_dexscreener_symbol_name(m) for m in batch])

        conn = get_connection()
        for mint, (symbol, name) in zip(batch, results):
            if symbol:
                conn.execute(
                    "UPDATE tokens SET symbol = ?, name = ? WHERE mint = ?",
                    (symbol, name or symbol, mint),
                )
                # created_at needed for Supabase sync; read it back
                row = conn.execute("SELECT created_at FROM tokens WHERE mint = ?", (mint,)).fetchone()
                created_at = int(row["created_at"]) if row and row["created_at"] else 0
                asyncio.create_task(sb.token(mint, symbol, name or symbol, created_at))
                fixed += 1
                logger.info("fixed %s..  → $%s (%s)", mint[:8], symbol, name or symbol)
        conn.commit()
        conn.close()

        logger.info("progress: %d/%d  fixed=%d", min(i + BATCH_SIZE, total), total, fixed)
        await asyncio.sleep(SLEEP_BETWEEN)

    logger.info("done — fixed %d of %d tokens (rest had no DexScreener listing)", fixed, total)
    # Give fire-and-forget supabase tasks a moment to flush
    await asyncio.sleep(3)


if __name__ == "__main__":
    asyncio.run(main())
