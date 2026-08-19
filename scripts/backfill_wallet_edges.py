"""Recompute post-graduation coordination from stored tapes, populating wallet_edges.

The typed wallet-pair graph only began persisting on 2026-08-19, and the funder
edge only reached the post-graduation pass at the same time. Every coin analysed
before that has entity rollups but no pairs, and its pairs would have been computed
without the strongest signal anyway.

This is a pure recompute over post_grad_swaps — no API calls. The existing
scripts/backfill_coordination.py is a different driver: it reads live_trades, which
covers a different (and much smaller) set of coins.

Re-runnable: upsert_coordination replaces both the entity rows and the edge rows for
each (mint, phase), so a second pass cannot double-count.

    uv run python scripts/backfill_wallet_edges.py [--limit N] [--min-swaps N]
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.analyzer.coordination import analyze_coin, upsert_coordination
from src.analyzer.distribution import _postgrad_edge_maps
from src.common.db import get_connection
from src.ingest.helius import Swap

MIN_SWAPS = 20          # below this there is no structure to find


def _tape(conn, mint: str) -> list[Swap]:
    """The coordination window only — see distribution.COORD_WINDOW_S."""
    from src.analyzer.distribution import COORD_WINDOW_S
    g = conn.execute(
        "SELECT graduated_at FROM graduation_events WHERE token_mint = ?", (mint,)).fetchone()
    lo = int(g[0]) if g else 0
    hi = lo + COORD_WINDOW_S if g else 1 << 62
    return [
        Swap(token_mint=mint, signer=r["wallet_address"], side=r["side"],
             sol_amount=float(r["sol_amount"] or 0.0),
             token_amount=float(r["token_amount"] or 0.0),
             timestamp=int(r["ts"]), slot=int(r["slot"] or 0), tx_signature="")
        for r in conn.execute(
            """SELECT ts, side, wallet_address, sol_amount, token_amount, slot
               FROM post_grad_swaps WHERE token_mint = ? AND ts BETWEEN ? AND ?
               ORDER BY ts""", (mint, lo, hi))
    ]


def main() -> None:
    argv = sys.argv
    limit = int(argv[argv.index("--limit") + 1]) if "--limit" in argv else 0
    min_swaps = int(argv[argv.index("--min-swaps") + 1]) if "--min-swaps" in argv else MIN_SWAPS

    conn = get_connection()
    q = """SELECT token_mint, COUNT(*) n FROM post_grad_swaps
           GROUP BY token_mint HAVING n >= ? ORDER BY n DESC"""
    mints = [r[0] for r in conn.execute(q, (min_swaps,))]
    if limit:
        mints = mints[:limit]
    print(f"recomputing coordination for {len(mints)} coins (>= {min_swaps} swaps)")

    t0, done, edges = time.time(), 0, 0
    for i, mint in enumerate(mints, 1):
        try:
            swaps = _tape(conn, mint)
            if len(swaps) < min_swaps:
                continue
            funder_map, fresh_map = _postgrad_edge_maps(mint, swaps, conn)
            cc = analyze_coin(mint, swaps, total_supply=None,
                              funder_by_wallet=funder_map, fresh=fresh_map)
            upsert_coordination(conn, cc, source="backfill", phase="postgrad")
            done += 1
            edges += len(cc.edges)
        except Exception as exc:
            print(f"  {mint[:10]} failed: {type(exc).__name__}: {exc}")
        if i % 200 == 0:
            print(f"  {i}/{len(mints)}  {done} done, {edges:,} pairs, "
                  f"{time.time()-t0:.0f}s")
    conn.close()
    print(f"\n{done} coins, {edges:,} typed pairs, {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
