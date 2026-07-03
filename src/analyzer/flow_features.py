"""BC order-flow features — one row per graduated mint, computed from the
already-fetched bonding-curve swap tape (zero extra API).

These are the structural inputs for the training dataset: how buying was
distributed across wallets, how front-loaded it was, and how much of it landed
in coordinated same-second bundles.
"""

from dataclasses import dataclass

from src.ingest.helius import Swap


@dataclass
class BcFlowFeatures:
    n_trades: int = 0
    n_buyers: int = 0
    n_sellers: int = 0
    buys_first_60s: int = 0
    same_second_bundle_count: int = 0   # seconds with buys from ≥2 distinct wallets
    top5_buyer_share: float = 0.0       # top-5 buyers' share of total SOL in [0,1]
    gini_buy_size: float = 0.0          # inequality of per-wallet SOL in [0,1]
    sol_in: float = 0.0
    sol_out: float = 0.0


def _gini(values: list[float]) -> float:
    """Gini coefficient of non-negative values (0 = equal, →1 = concentrated)."""
    vals = sorted(v for v in values if v > 0)
    n = len(vals)
    total = sum(vals)
    if n < 2 or total <= 0:
        return 0.0
    weighted = sum((i + 1) * v for i, v in enumerate(vals))
    return round((2 * weighted) / (n * total) - (n + 1) / n, 4)


def compute_bc_flow_features(
    bc_swaps: list[Swap], token_created_at: int
) -> BcFlowFeatures:
    """Aggregate a BC swap tape (structural signers already excluded upstream)."""
    buys = [s for s in bc_swaps if s.side == "buy"]
    sells = [s for s in bc_swaps if s.side == "sell"]

    sol_by_buyer: dict[str, float] = {}
    buys_by_second: dict[int, set[str]] = {}
    buys_first_60s = 0
    for s in buys:
        sol_by_buyer[s.signer] = sol_by_buyer.get(s.signer, 0.0) + s.sol_amount
        buys_by_second.setdefault(s.timestamp, set()).add(s.signer)
        if 0 <= s.timestamp - token_created_at <= 60:
            buys_first_60s += 1

    sol_in = sum(sol_by_buyer.values())
    top5 = sorted(sol_by_buyer.values(), reverse=True)[:5]
    return BcFlowFeatures(
        n_trades=len(bc_swaps),
        n_buyers=len(sol_by_buyer),
        n_sellers=len({s.signer for s in sells}),
        buys_first_60s=buys_first_60s,
        same_second_bundle_count=sum(
            1 for wallets in buys_by_second.values() if len(wallets) >= 2
        ),
        top5_buyer_share=round(sum(top5) / sol_in, 4) if sol_in > 0 else 0.0,
        gini_buy_size=_gini(list(sol_by_buyer.values())),
        sol_in=round(sol_in, 4),
        sol_out=round(sum(s.sol_amount for s in sells), 4),
    )


def upsert_bc_flow_features(conn, token_mint: str, f: BcFlowFeatures) -> None:
    conn.execute(
        """INSERT INTO bc_flow_features
               (token_mint, n_trades, n_buyers, n_sellers, buys_first_60s,
                same_second_bundle_count, top5_buyer_share, gini_buy_size,
                sol_in, sol_out)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(token_mint) DO UPDATE SET
               n_trades = excluded.n_trades,
               n_buyers = excluded.n_buyers,
               n_sellers = excluded.n_sellers,
               buys_first_60s = excluded.buys_first_60s,
               same_second_bundle_count = excluded.same_second_bundle_count,
               top5_buyer_share = excluded.top5_buyer_share,
               gini_buy_size = excluded.gini_buy_size,
               sol_in = excluded.sol_in,
               sol_out = excluded.sol_out""",
        (
            token_mint, f.n_trades, f.n_buyers, f.n_sellers, f.buys_first_60s,
            f.same_second_bundle_count, f.top5_buyer_share, f.gini_buy_size,
            f.sol_in, f.sol_out,
        ),
    )
    conn.commit()
