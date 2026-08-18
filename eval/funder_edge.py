"""Does entering only on slow-exit-funder launches beat following every team?

NEGATIVE_RESULTS #19 measured the base case: copying team buys returns 0.948x per
round trip, and latency is not why — even 0s execution loses. This asks whether
CONDITIONING on the operator changes that, which is the one version of the thesis
with a mechanism behind it (same funder, same playbook) and the one with measured
persistence: exit timing is a funder trait, split-half Spearman +0.437 (p=0.008)
across 36 repeat funders, and funders ranked slow on their early coins go on to
peak 2.68x against 1.48x for fast ones.

DISCIPLINE, because this is exactly the shape of test that flatters itself:

  * POINT-IN-TIME reputation. A funder's classification for a coin uses ONLY their
    coins that graduated strictly earlier. No coin contributes to its own label.
  * The reputation split is fixed on the TRAINING era and applied unchanged to
    later coins, so the threshold is not fitted to the returns it is judged on.
  * Fills, lag and fees are inherited from src/analyzer/follow_return.py — the same
    machinery that produced the 0.948x baseline, so the comparison is like-for-like.
  * Bootstrapped BY FUNDER. Trades within a funder are correlated; resampling
    trades would shrink the interval and manufacture significance.
  * Report the MEAN. A 55.4% win rate carried a 0.963 mean in #19 — win rate and
    median are uninterpretable in an asset defined by its left tail.

    uv run python -m eval.funder_edge
"""

from __future__ import annotations

import sys
from collections import defaultdict

import numpy as np

MIN_PRIOR_COINS = 2        # a funder needs this much history before it is classifiable
MIN_TRADES = 25            # per arm, below this no claim
BOOTSTRAP_DRAWS = 3000


def _returns_for_coin(conn, mint: str) -> list[float]:
    """Realised follow-return per team buy, from the shared implementation."""
    from src.analyzer.follow_return import follow_returns_for_coin
    return [m for _w, _ts, m in follow_returns_for_coin(conn, mint)]


def collect(conn) -> dict:
    coins = conn.execute(
        """SELECT tc.funding_source AS funder, ct.token_mint AS mint,
                  ge.graduated_at AS grad, ct.time_to_team_exit_s AS tx
           FROM team_clusters tc
           JOIN coin_trajectory ct USING(token_mint)
           JOIN graduation_events ge USING(token_mint)
           JOIN tokens t ON t.mint = ct.token_mint
           WHERE t.platform = 'pump.fun' AND ct.n_price_points >= 30
             AND tc.funding_source IS NOT NULL
             AND ct.time_to_team_exit_s IS NOT NULL
           ORDER BY ge.graduated_at""").fetchall()

    # Fix the slow/fast boundary on the FIRST HALF of history only, then apply it
    # unchanged. Choosing it on all data would tune the threshold to the returns.
    cut = len(coins) // 2
    train_exits = [r["tx"] for r in coins[:cut]]
    boundary = float(np.median(train_exits)) if train_exits else 0.0

    history: dict[str, list[float]] = defaultdict(list)
    arms: dict[str, list[tuple[str, float]]] = {"all": [], "slow": [], "fast": []}

    for r in coins:
        prior = history[r["funder"]]
        # classify from prior coins ONLY, then record this coin's outcome
        if len(prior) >= MIN_PRIOR_COINS:
            arm = "slow" if float(np.median(prior)) >= boundary else "fast"
            for m in _returns_for_coin(conn, r["mint"]):
                arms[arm].append((r["funder"], m))
                arms["all"].append((r["funder"], m))
        history[r["funder"]].append(float(r["tx"]))

    return {"arms": arms, "boundary": boundary, "n_coins": len(coins)}


def summarise(pairs: list[tuple[str, float]], seed: int = 0) -> dict | None:
    if len(pairs) < MIN_TRADES:
        return None
    vals = np.array([m for _f, m in pairs], dtype=float)
    by_funder: dict[str, list[float]] = defaultdict(list)
    for f, m in pairs:
        by_funder[f].append(m)
    keys = list(by_funder)
    rng = np.random.default_rng(seed)
    means = []
    for _ in range(BOOTSTRAP_DRAWS):
        pick = rng.choice(len(keys), len(keys), replace=True)
        pool = [x for i in pick for x in by_funder[keys[i]]]
        if pool:
            means.append(float(np.mean(pool)))
    a = np.array(means)
    return {"n": len(vals), "funders": len(keys), "mean": float(vals.mean()),
            "lo": float(np.percentile(a, 2.5)), "hi": float(np.percentile(a, 97.5)),
            "p_lose": float((a <= 1.0).mean())}


def main() -> int:
    from src.common.db import get_connection

    conn = get_connection()
    d = collect(conn)
    conn.close()

    print("FOLLOWING TEAM BUYS, CONDITIONED ON THE FUNDER'S PRIOR RECORD")
    print(f"slow/fast boundary fixed on the training era: {d['boundary']:.0f}s "
          f"median exit  ({d['n_coins']} coins)\n")
    print(f"{'arm':>26} {'trades':>7} {'funders':>8} {'mean':>8} "
          f"{'95% CI':>20} {'P(loses)':>9}")
    print("-" * 82)
    for arm in ("all", "fast", "slow"):
        s = summarise(d["arms"][arm], seed=hash(arm) % 1000)
        label = {"all": "all classifiable teams",
                 "fast": "FAST-exit funders only",
                 "slow": "SLOW-exit funders only"}[arm]
        if s is None:
            print(f"{label:>26} {len(d['arms'][arm]):>7}   — under {MIN_TRADES}, no claim")
            continue
        ci = f"[{s['lo']:.3f}, {s['hi']:.3f}]"
        print(f"{label:>26} {s['n']:>7} {s['funders']:>8} {s['mean']:>8.3f} "
              f"{ci:>20} {s['p_lose']:>8.1%}")
    print("\nbaseline for comparison: 0.948x following every team (NEGATIVE_RESULTS #19)")
    print("1.000 = breakeven, after 1.5% fees, before slippage")
    return 0


if __name__ == "__main__":
    sys.exit(main())
