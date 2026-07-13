"""Early on-chain ATTENTION — crowd arrival in the first 5 minutes after graduation.

Measures the crowd directly off the order flow: how many distinct wallets showed
up, whether they are still arriving or already leaving (accel), how much of the net
inflow is retail rather than the team. Computed from the swap tape `_do_early_check`
already fetches, so the marginal cost is zero requests and zero API keys.

WHAT IT IS FOR — survival, and only survival:

    survive>=60min, coins still alive at T+5m   ROC 0.904   top-5% survive 100%
    survive>=60min, structure @T+0 (reference)  ROC 0.806

Both leak-audited: dropping price_run does not degrade either (0.907 -> 0.913), so
neither depends on already-visible price action.

WHAT IT IS **NOT** FOR — the pump. This module was built on the hypothesis that the
10x is a crowd phenomenon invisible to structure, and that hypothesis FAILED:

    reached_10x, all features                     ROC 0.746  <- LEAKY, not a result
    reached_10x, price_run removed                ROC 0.623
    reached_10x, only 10x still FUTURE at T+5m    ROC 0.592
    reached_10x, both corrections                 ROC 0.517  <- a coin flip

The 0.746 was `price_run` leaking the label: 36% of coins that reached 10x did so
INSIDE the 5-minute window, so the feature contained the answer. Structure scores
0.583 on the same target. The pump is therefore unpredictable from BOTH graduation
structure AND early order flow — two independent negative results.

That also settles the social-media question. Crowd arrival measured on-chain is a
*better* attention signal than any follower count (direct, unfakeable, free), and it
does not predict the pump. A lagging, gameable proxy for a quantity we already
measure directly and which already failed is not worth an API bill.

Never add a moon/10x head here. Anything that only fires once the pump is visible in
the price is detection, not discrimination, and has no value.

Leak rule: these are features of the FIRST `window_s` seconds only. Callers must
never widen the window past the moment the prediction is made.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

# The prediction moment. 5 minutes is early enough to precede the median collapse
# (10.5 min) and the median team exit, and late enough for the crowd to be visible.
DEFAULT_WINDOW_S = 300

MIN_TRADES = 3      # below this the tape is too thin to read anything


@dataclass
class EarlyAttention:
    """Order-flow attention over the first `window_s` after graduation."""
    n_trades: int = 0
    n_wallets: int = 0
    buy_ratio: float = 0.0            # share of trades that are buys
    trades_per_wallet: float = 0.0    # <1.3 = broad crowd; high = few wallets churning
    buy_sol: float = 0.0
    net_sol: float = 0.0              # buy volume - sell volume
    price_run: float = 1.0            # peak / first print, within the window
    accel: float = 1.0                # trades in 2nd half / trades in 1st half
    new_wallet_rate: float = 0.0      # wallets whose first trade is in the 2nd half
    max_buy_sol: float = 0.0
    retail_net_sol: float = 0.0       # net flow EXCLUDING known team wallets
    team_sold: int = 0


def compute_early_attention(
    swaps,                            # objects with .timestamp .side .signer .sol_amount .price_usd
    graduated_at: int,
    team: set[str] | None = None,
    window_s: int = DEFAULT_WINDOW_S,
) -> EarlyAttention | None:
    """Measure crowd arrival in the first `window_s`. Pure. None if the tape is too thin."""
    team = team or set()
    cut = graduated_at + window_s
    win = sorted(
        (s for s in swaps if graduated_at <= s.timestamp <= cut),
        key=lambda s: s.timestamp,
    )
    if len(win) < MIN_TRADES:
        return None

    a = EarlyAttention()
    a.n_trades = len(win)

    wallets = {s.signer for s in win if s.signer}
    a.n_wallets = len(wallets)
    a.trades_per_wallet = a.n_trades / max(a.n_wallets, 1)

    buys = [s for s in win if s.side == "buy"]
    sells = [s for s in win if s.side == "sell"]
    a.buy_ratio = len(buys) / a.n_trades

    a.buy_sol = sum(s.sol_amount or 0.0 for s in buys)
    sell_sol = sum(s.sol_amount or 0.0 for s in sells)
    a.net_sol = a.buy_sol - sell_sol
    a.max_buy_sol = max((s.sol_amount or 0.0 for s in buys), default=0.0)

    retail_buy = sum(s.sol_amount or 0.0 for s in buys if s.signer not in team)
    retail_sell = sum(s.sol_amount or 0.0 for s in sells if s.signer not in team)
    a.retail_net_sol = retail_buy - retail_sell
    a.team_sold = int(any(s.signer in team for s in sells))

    priced = [s.price_usd for s in win if s.price_usd and s.price_usd > 0]
    if priced:
        first = next(s.price_usd for s in win if s.price_usd and s.price_usd > 0)
        if first > 0:
            a.price_run = max(priced) / first

    # Acceleration: is the crowd still arriving, or already leaving? The halves are
    # split on TIME, not trade count, so a dead second half correctly reads as decay.
    mid = graduated_at + window_s / 2
    first_half = [s for s in win if s.timestamp < mid]
    second_half = [s for s in win if s.timestamp >= mid]
    a.accel = len(second_half) / max(len(first_half), 1)

    seen_early = {s.signer for s in first_half if s.signer}
    fresh = {s.signer for s in second_half if s.signer} - seen_early
    a.new_wallet_rate = len(fresh) / max(a.n_wallets, 1)

    return a


def to_features(a: EarlyAttention, prefix: str = "e5_") -> dict:
    """Flatten to the prefixed feature dict the model artifact is keyed on."""
    return {f"{prefix}{k}": float(v) for k, v in asdict(a).items()}


def upsert_early_attention(conn, token_mint: str, window_s: int, a: EarlyAttention) -> None:
    import time
    d = asdict(a)
    cols = ["token_mint", "window_s", "computed_at", *d.keys()]
    vals = [token_mint, window_s, int(time.time()), *d.values()]
    ph = ",".join("?" * len(cols))
    conn.execute(
        f"INSERT OR REPLACE INTO early_attention ({','.join(cols)}) VALUES ({ph})",
        vals,
    )
