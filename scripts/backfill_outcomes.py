"""Backfill coin_outcomes for graduation_events that have no outcome records.

For the 587 tokens that graduated before the outcome tracker was wired into
the graduation monitor, this script fetches the current price state for each
and records it. Tokens that are rugged will show rug; survivors will show ok or moon.

Run on Mac mini:
    uv run python scripts/backfill_outcomes.py
    uv run python scripts/backfill_outcomes.py --dry-run  (preview only)
"""

import asyncio
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.common.db import get_connection
from src.analyzer.outcome_tracker import _do_check, _fetch_current_mc, _classify, _save_outcome, _recompute_wallet_scores

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

DRY_RUN = "--dry-run" in sys.argv
BATCH_SIZE = 20          # process N tokens concurrently
SLEEP_BETWEEN = 0.5      # seconds between batches


async def backfill_token(
    mint: str,
    bc_holders_json: str,
    graduated_at: int,
    dry_run: bool,
) -> str:
    """Fetch current state and record as outcome. Returns classification."""
    # Pump.fun always graduates at ~$69K USD market cap
    launch_mc = 69_000.0

    current_mc = await _fetch_current_mc(mint)
    outcome = _classify(mint, offset_h=24, launch_mc=launch_mc, current_mc=current_mc)

    if dry_run:
        logger.info(
            "DRY %s..  launch=%.0f  current=%.0f  → %s",
            mint[:8],
            launch_mc or 0,
            current_mc or 0,
            outcome.classified or "unknown",
        )
        return outcome.classified or "unknown"

    conn = get_connection()
    try:
        # Only write if not already recorded
        exists = conn.execute(
            "SELECT 1 FROM coin_outcomes WHERE token_mint = ? AND check_offset_h = 24",
            (mint,),
        ).fetchone()
        if not exists:
            _save_outcome(outcome, conn)
            if outcome.classified:
                await _recompute_wallet_scores(mint, conn)

            # Sync to Supabase
            from src.common import supabase_sync as sb
            asyncio.create_task(sb.coin_outcome(
                token_mint=mint,
                check_offset_h=24,
                checked_at=outcome.checked_at,
                mc_usd=outcome.mc_usd,
                price_change_pct=outcome.price_change_pct,
                classified=outcome.classified,
            ))

            logger.info(
                "backfilled %s..  → %s  (graduated %dh ago)",
                mint[:8],
                outcome.classified or "unknown",
                int((time.time() - graduated_at) / 3600),
            )
        else:
            logger.debug("skip %s.. — outcome already exists", mint[:8])
    finally:
        conn.close()

    return outcome.classified or "unknown"


async def main() -> None:
    conn = get_connection()

    # Find tokens with no 24h outcome
    rows = conn.execute(
        """SELECT ge.token_mint, ge.bc_top_holders_json, ge.graduated_at
           FROM graduation_events ge
           WHERE ge.structural_verdict IS NOT NULL
             AND NOT EXISTS (
               SELECT 1 FROM coin_outcomes co
               WHERE co.token_mint = ge.token_mint AND co.check_offset_h = 24
             )
           ORDER BY ge.graduated_at DESC""",
    ).fetchall()
    conn.close()

    total = len(rows)
    if total == 0:
        logger.info("no tokens to backfill — all have 24h outcomes already")
        return

    logger.info(
        "backfilling %d tokens%s...",
        total,
        " (DRY RUN)" if DRY_RUN else "",
    )

    results: dict[str, int] = {"moon": 0, "ok": 0, "rug": 0, "unknown": 0}

    for i in range(0, total, BATCH_SIZE):
        batch = rows[i : i + BATCH_SIZE]
        classifications = await asyncio.gather(
            *[
                backfill_token(
                    r["token_mint"],
                    r["bc_top_holders_json"],
                    r["graduated_at"],
                    DRY_RUN,
                )
                for r in batch
            ],
            return_exceptions=True,
        )
        for c in classifications:
            if isinstance(c, Exception):
                results["unknown"] += 1
            else:
                results[c] += 1

        logger.info(
            "progress: %d/%d  moon=%d  ok=%d  rug=%d  unknown=%d",
            min(i + BATCH_SIZE, total), total,
            results["moon"], results["ok"], results["rug"], results["unknown"],
        )
        await asyncio.sleep(SLEEP_BETWEEN)

    logger.info(
        "\nBackfill complete — moon=%d  ok=%d  rug=%d  unknown=%d",
        results["moon"], results["ok"], results["rug"], results["unknown"],
    )

    if not DRY_RUN:
        logger.info(
            "rug rate: %.1f%%  moon rate: %.1f%%",
            results["rug"] / max(total, 1) * 100,
            results["moon"] / max(total, 1) * 100,
        )


if __name__ == "__main__":
    asyncio.run(main())
