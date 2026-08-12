"""Populate wallet_follow_returns + wallet_follow_stats from the stored tape.

Pure recompute — no API calls. Safe to re-run; rows are keyed on
(wallet, mint, bought_at) so a second pass overwrites rather than duplicates.

Only anchor-valid coins are used: a coin whose tape opens late cannot price an
entry fill, and its returns would be measured from the wrong zero.

    uv run python scripts/rebuild_follow_returns.py [--limit N]
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.analyzer.follow_return import (
    follow_returns_for_coin, rebuild_wallet_follow_stats, upsert_follow_returns,
)
from src.common.db import get_connection


def main() -> None:
    limit = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else 0
    conn = get_connection()
    q = """SELECT ct.token_mint FROM coin_trajectory ct
           JOIN graduation_events ge USING(token_mint)
           JOIN tokens t ON t.mint = ct.token_mint
           WHERE t.platform='pump.fun' AND COALESCE(ge.is_manufactured,0)=0
             AND ct.n_price_points >= 30
             AND (SELECT MIN(p.ts) FROM post_grad_swaps p
                  WHERE p.token_mint = ct.token_mint)
                 BETWEEN ge.graduated_at AND ge.graduated_at + 120
           ORDER BY ge.graduated_at DESC"""
    if limit:
        q += f" LIMIT {limit}"
    coins = [r[0] for r in conn.execute(q)]
    print(f"computing follow-returns over {len(coins)} anchor-valid coins")

    total = 0
    for i, m in enumerate(coins, 1):
        total += upsert_follow_returns(conn, m, follow_returns_for_coin(conn, m))
        if i % 100 == 0:
            conn.commit()
            print(f"  {i}/{len(coins)}  {total} trades")
    conn.commit()

    n = rebuild_wallet_follow_stats(conn)
    conn.commit()
    row = conn.execute(
        """SELECT COUNT(*), AVG(mean_multiple) FROM wallet_follow_stats
           WHERE n_trades >= 3""").fetchone()
    print(f"\n{total} follow-trades over {len(coins)} coins -> {n} wallets scored")
    print(f"wallets with >=3 trades: {row[0]}   their mean multiple: {row[1]:.4f}"
          if row[0] else "no wallet has >=3 trades yet")
    conn.close()


if __name__ == "__main__":
    main()
