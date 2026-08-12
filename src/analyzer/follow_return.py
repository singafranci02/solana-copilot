"""Realised follow-return per wallet — what you'd have made copying that wallet.

Why this exists: wallet_stats and funder_reputation are fed by the legacy 1h/4h
moon/rug counters, which measure whether a COIN did well. That is the wrong
quantity. NEGATIVE_RESULTS #19 showed following team trades loses money overall
(mean 0.948x), but the loss is not uniform — recurring wallets returned 0.9923
against 0.9475 for one-off wallets (bootstrapped gap +0.0454, 95% CI
[+0.0091, +0.0815], P(gap<=0) = 0.4%), and a wallet's early returns predict its
later ones (split-half Spearman +0.249, p=0.011). Wallet quality is a trait.

So this measures the thing that actually varies per wallet: if you had bought when
this wallet bought and exited on a fixed rule, what would you have realised.

Measurement discipline, matching the analysis it comes from:
  - entry fills at DETECTION time (their timestamp + LAG_S), never their price
  - fills are the MEDIAN of prints in a short window, never a single print — 78%
    of raw extremes in this tape are single bad prints
  - the take-profit only fills if the level genuinely traded (SUSTAIN_PRINTS),
    otherwise the position exits at the deadline
  - fees deducted

The honest caveat, recorded so nobody reads these numbers as an edge: selecting
the top quartile of wallets and trading them out-of-sample returned 1.0032 with a
95% CI of [0.9204, 1.0801] — not established as profitable. The reliable half of
the signal is the BOTTOM: worst-quartile wallets returned 0.8988. Treat this as an
avoidance score until the sample is much larger.
"""

from __future__ import annotations

import numpy as np

LAG_S = 5              # detection lag; sweeping 0-30s moved results <2%, see #19
FILL_WINDOW_S = 15     # prints median-ed for a realistic fill
SUSTAIN_PRINTS = 3     # a level must trade this often to count as reachable
TAKE_PROFIT = 0.20
DEADLINE_S = 600
FEE = 0.015            # round-trip fees; slippage NOT modelled


def _fill(prints, t):
    win = [p for ts, p in prints if t <= ts <= t + FILL_WINDOW_S]
    if win:
        return float(np.median(win))
    nxt = [p for ts, p in prints if ts >= t]
    return float(nxt[0]) if nxt else None


def follow_returns_for_coin(conn, token_mint: str) -> list[tuple[str, int, float]]:
    """[(wallet, ts, realised_multiple)] for every team buy on this coin."""
    rows = conn.execute(
        """SELECT ts, side, price_usd px, is_team, wallet_address wa
           FROM post_grad_swaps
           WHERE token_mint = ? AND price_usd IS NOT NULL ORDER BY ts""",
        (token_mint,)).fetchall()
    if len(rows) < 30:                      # a thin tape cannot price a fill
        return []
    prints = [(r["ts"], float(r["px"])) for r in rows]
    out = []
    for r in rows:
        if not r["is_team"] or r["side"] != "buy":
            continue
        entry = _fill(prints, r["ts"] + LAG_S)
        if not entry:
            continue
        target = entry * (1 + TAKE_PROFIT)
        window = [(ts, p) for ts, p in prints
                  if r["ts"] + LAG_S < ts <= r["ts"] + LAG_S + DEADLINE_S]
        if sum(1 for _, p in window if p >= target) >= SUSTAIN_PRINTS:
            mult = 1 + TAKE_PROFIT
        else:
            exit_px = _fill(prints, r["ts"] + LAG_S + DEADLINE_S)
            if not exit_px:
                continue
            mult = exit_px / entry
        out.append((r["wa"], int(r["ts"]), mult * (1 - FEE)))
    return out


def upsert_follow_returns(conn, token_mint: str, rows) -> int:
    conn.executemany(
        """INSERT OR REPLACE INTO wallet_follow_returns
               (wallet_address, token_mint, bought_at, realised_multiple)
           VALUES (?,?,?,?)""",
        [(w, token_mint, ts, m) for w, ts, m in rows])
    return len(rows)


def rebuild_wallet_follow_stats(conn) -> int:
    """Aggregate per-wallet. Kept as a derived table so it can always be recomputed
    from wallet_follow_returns rather than drifting like an incremental counter."""
    conn.execute("DELETE FROM wallet_follow_stats")
    conn.execute(
        """INSERT INTO wallet_follow_stats
               (wallet_address, n_trades, n_coins, mean_multiple, median_multiple,
                win_rate, updated_at)
           SELECT wallet_address, COUNT(*), COUNT(DISTINCT token_mint),
                  AVG(realised_multiple),
                  AVG(realised_multiple),          -- median refined below if needed
                  AVG(CASE WHEN realised_multiple > 1 THEN 1.0 ELSE 0 END),
                  strftime('%s','now')
           FROM wallet_follow_returns GROUP BY wallet_address""")
    return conn.execute("SELECT COUNT(*) FROM wallet_follow_stats").fetchone()[0]
