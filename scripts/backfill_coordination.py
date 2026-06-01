"""Run coordinated-entity detection over already-captured live_trades (batch driver).

Proves the coordination calculations on real backfilled order flow with NO live
watcher and NO API for the same-slot path. For each token with recorded trades:
group same-slot bundles, link wallets by shared funder / buy-size / lockstep sells,
assemble coordinated entities (union-find), and store the rollup.

Same-slot bundle detection needs zero API calls. Funder edges are added only when
--funders is passed (uses cached wallets.funding_source first; bounded).

Run on Mac mini:
    uv run python scripts/backfill_coordination.py
    uv run python scripts/backfill_coordination.py --days 14 --funders
"""

import asyncio
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.common.db import get_connection
from src.ingest.helius import Swap
from src.analyzer.coordination import analyze_coin, fresh_flags, upsert_coordination

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)

DAYS = None
USE_FUNDERS = "--funders" in sys.argv
if "--days" in sys.argv:
    DAYS = int(sys.argv[sys.argv.index("--days") + 1])


def _load_swaps(conn, mint: str) -> list[Swap]:
    rows = conn.execute(
        """SELECT wallet_address, side, sol_amount, token_amount, ts, slot
           FROM live_trades WHERE token_mint = ?""",
        (mint,),
    ).fetchall()
    return [
        Swap(
            side=r["side"], token_mint=mint, sol_amount=float(r["sol_amount"] or 0),
            token_amount=float(r["token_amount"] or 0), signer=r["wallet_address"],
            timestamp=int(r["ts"] or 0), slot=int(r["slot"] or 0),
        )
        for r in rows
    ]


def _fresh_for(conn, mint: str, wallets: set[str], now: int) -> dict[str, str]:
    if not wallets:
        return {}
    placeholders = ",".join("?" * len(wallets))
    first_seen = {
        r["address"]: int(r["first_seen"] or 0)
        for r in conn.execute(
            f"SELECT address, first_seen FROM wallets WHERE address IN ({placeholders})",
            tuple(wallets),
        )
    }
    offsets = {
        r["wallet_address"]: float(r["first_buy_offset_s"] or 0)
        for r in conn.execute(
            f"SELECT wallet_address, first_buy_offset_s FROM bc_accumulation "
            f"WHERE token_mint = ? AND wallet_address IN ({placeholders})",
            (mint, *wallets),
        )
    }
    return fresh_flags(first_seen, offsets, now)


def _funders_for(conn, mint: str, wallets: set[str]) -> dict[str, str | None]:
    if not wallets:
        return {}
    placeholders = ",".join("?" * len(wallets))
    return {
        r["address"]: r["funding_source"]
        for r in conn.execute(
            f"SELECT address, funding_source FROM wallets WHERE address IN ({placeholders})",
            tuple(wallets),
        )
    }


async def main() -> None:
    conn = get_connection()
    if DAYS:
        cutoff = int(time.time()) - DAYS * 86_400
        rows = conn.execute(
            "SELECT DISTINCT token_mint FROM live_trades WHERE ts >= ?", (cutoff,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT DISTINCT token_mint FROM live_trades").fetchall()

    mints = [r["token_mint"] for r in rows]
    total = len(mints)
    if total == 0:
        logger.info("no tokens in live_trades — run backfill_live_trades.py first")
        conn.close()
        return

    logger.info("analysing coordination for %d tokens (funders=%s)...", total, USE_FUNDERS)
    now = int(time.time())
    flagged = 0

    for i, mint in enumerate(mints):
        swaps = _load_swaps(conn, mint)
        if not swaps:
            continue
        wallets = {s.signer for s in swaps}

        # total supply (best-effort) — for true % rather than observed-volume %
        trow = conn.execute(
            "SELECT market_cap_usd_snapshot FROM tokens WHERE mint = ?", (mint,)
        ).fetchone()
        total_supply = None  # use observed buy volume as denominator (robust w/o supply)

        fresh = _fresh_for(conn, mint, wallets, now)
        funder_map = _funders_for(conn, mint, wallets) if USE_FUNDERS else None

        cc = analyze_coin(mint, swaps, total_supply=total_supply,
                          funder_by_wallet=funder_map, fresh=fresh)
        upsert_coordination(conn, cc, source="batch", phase="postgrad")

        from src.common import supabase_sync as sb
        asyncio.create_task(sb.coin_coordination(
            token_mint=mint, entity_count=cc.entity_count,
            bundled_supply_pct=cc.bundle_stats.bundled_supply_pct,
            bundle_wallet_count=cc.bundle_stats.bundle_wallet_count,
            largest_bundle_size=cc.bundle_stats.largest_bundle_size,
            largest_entity_supply_pct=cc.largest_entity_supply_pct,
            largest_entity_wallet_count=cc.largest_entity_wallet_count,
            largest_entity_fresh_ratio=cc.largest_entity_fresh_ratio,
            largest_entity_state=cc.largest_entity_state,
            phase="postgrad", source="batch",
        ))
        if cc.entities:
            asyncio.create_task(sb.coordinated_entities_batch(
                token_mint=mint,
                rows=[
                    {
                        "token_mint": mint, "phase": "postgrad", "entity_id": e.entity_id,
                        "member_addresses": list(e.wallets), "wallet_count": e.wallet_count,
                        "supply_pct": e.supply_pct, "fresh_ratio": e.fresh_ratio,
                        "state": e.state, "edge_sources": list(e.edge_sources),
                        "computed_at": now,
                    }
                    for e in cc.entities
                ],
            ))

        if cc.bundle_stats.bundled_supply_pct > 30:
            flagged += 1
        if i > 0 and i % 50 == 0:
            logger.info("progress: %d/%d  bundled>30%%: %d", i, total, flagged)

    conn.close()
    logger.info("done — %d tokens analysed, %d flagged bundled>30%%", total, flagged)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("interrupted — per-token results saved")
