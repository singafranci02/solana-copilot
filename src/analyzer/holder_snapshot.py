"""Holder-base trajectory + new-entrant detection (post-graduation).

Distinguishes organic growth (new real holders entering) from team churn, and
flags smart money entering after graduation — a bullish continuation signal.

Pure functions; all IO (Helius top-holders, DexScreener price) is done by the
caller in distribution._do_check and passed in.
"""

from dataclasses import dataclass


@dataclass
class HolderSnapshotMetrics:
    holder_count: int
    top10_pct: float
    new_holder_count: int
    churned_holder_count: int


@dataclass
class NewEntrant:
    wallet: str
    is_smart_money: bool


def compute_holder_snapshot(
    accounts: list[dict],
    grad_holder_set: set[str],
    total_supply: float,
) -> HolderSnapshotMetrics:
    """Compute holder-base metrics from a current top-holders snapshot.

    accounts: current top holders (Helius getTokenLargestAccounts, capped ~20),
              each {address, uiAmount}.
    grad_holder_set: addresses that held at graduation (top-N snapshot).
    """
    current = {a.get("address") for a in accounts if a.get("address")}
    holder_count = len(current)

    if total_supply > 0:
        top10 = sorted(
            (float(a.get("uiAmount") or 0) for a in accounts), reverse=True
        )[:10]
        top10_pct = round(sum(top10) / total_supply * 100, 2)
    else:
        top10_pct = 0.0

    new_holders = current - grad_holder_set
    churned = grad_holder_set - current

    return HolderSnapshotMetrics(
        holder_count=holder_count,
        top10_pct=top10_pct,
        new_holder_count=len(new_holders),
        churned_holder_count=len(churned),
    )


def detect_new_entrants(
    swap_wallets: set[str],
    grad_holder_set: set[str],
    smart_money_set: set[str],
) -> list[NewEntrant]:
    """Wallets that traded post-grad but were NOT graduation holders.

    Flags each as smart money if present in smart_money_set. New smart money
    entering after graduation is a bullish continuation signal.
    """
    entrants = swap_wallets - grad_holder_set
    return [
        NewEntrant(wallet=w, is_smart_money=w in smart_money_set)
        for w in sorted(entrants)
    ]
