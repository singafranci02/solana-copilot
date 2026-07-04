"""Transaction-level post-graduation team behaviour tracking.

The distribution tracker only compares holder-balance snapshots. This module
reconstructs the actual buy/sell timeline for a graduated coin's team cluster
wallets so we can see HOW a team operates the coin: pump, slow distribute, or
coordinated dump.

Reuses src/ingest/helius.py:
  - get_transactions_for_address(addr, limit) → enhanced txs
  - parse_swap(raw_tx) → Swap(side, token_mint, sol_amount, token_amount,
                              signer, timestamp, slot)

Record + display only — these metrics do NOT feed structural_read yet.
"""

import asyncio
import logging
from dataclasses import dataclass

from src.ingest.helius import Swap, parse_swap

logger = logging.getLogger(__name__)

COORDINATED_WINDOW_S = 300   # 5 minutes — sells from ≥2 distinct wallets within this window


@dataclass
class SwapMetrics:
    team_buy_count: int = 0
    team_sell_count: int = 0
    team_net_sol: float = 0.0          # sell SOL − buy SOL (positive = net outflow to team)
    snipers_sold_pct: float | None = None
    coordinated_sell_count: int = 0


# ── pure functions ─────────────────────────────────────────────────────────────

def dedup_swaps(swaps: list[Swap]) -> list[Swap]:
    """Collapse swaps on (token_mint, signer, slot, side), keeping the first seen."""
    seen: set[tuple[str, str, int, str]] = set()
    out: list[Swap] = []
    for s in swaps:
        key = (s.token_mint, s.signer, s.slot, s.side)
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def filter_token_swaps_window(
    swaps: list[Swap], token_mint: str, lo_ts: int, hi_ts: float = float("inf")
) -> list[Swap]:
    """Keep swaps for this token within [lo_ts, hi_ts] (inclusive)."""
    return [
        s for s in swaps
        if s.token_mint == token_mint and lo_ts <= s.timestamp <= hi_ts
    ]


def filter_token_swaps(swaps: list[Swap], token_mint: str, since_ts: int) -> list[Swap]:
    """Keep only swaps for this token at or after the graduation timestamp."""
    return filter_token_swaps_window(swaps, token_mint, since_ts)


def price_sol(swap: Swap) -> float | None:
    """SOL price per token for a swap, or None if token_amount is zero."""
    if swap.token_amount and swap.token_amount > 0:
        return swap.sol_amount / swap.token_amount
    return None


def coordinated_sell_windows(
    swaps: list[Swap], window_s: int = COORDINATED_WINDOW_S
) -> list[set[str]]:
    """Return the wallet set of each window in which ≥2 distinct wallets sold."""
    sells = sorted((s for s in swaps if s.side == "sell"), key=lambda s: s.timestamp)
    windows: list[set[str]] = []
    i = 0
    n = len(sells)
    while i < n:
        window_start = sells[i].timestamp
        wallets: set[str] = set()
        j = i
        while j < n and sells[j].timestamp - window_start <= window_s:
            wallets.add(sells[j].signer)
            j += 1
        if len(wallets) >= 2:
            windows.append(wallets)
        i = j
    return windows


def detect_coordinated_sells(swaps: list[Swap], window_s: int = COORDINATED_WINDOW_S) -> int:
    """Count time windows in which ≥2 distinct wallets sold (int wrapper)."""
    return len(coordinated_sell_windows(swaps, window_s))


def compute_metrics(
    swaps: list[Swap],
    grad_positions: dict[str, float],
    sniper_wallets: set[str] | None = None,
) -> SwapMetrics:
    """Aggregate team swap behaviour into a SwapMetrics.

    Args:
        swaps: deduped, token-filtered team swaps.
        grad_positions: wallet → token holding at graduation (denominator for sold %).
        sniper_wallets: subset of wallets flagged as BC snipers; if None, all are snipers.
    """
    buys = [s for s in swaps if s.side == "buy"]
    sells = [s for s in swaps if s.side == "sell"]

    buy_sol = sum(s.sol_amount for s in buys)
    sell_sol = sum(s.sol_amount for s in sells)

    # snipers_sold_pct — supply-weighted % of graduation position sold by snipers
    snipers = sniper_wallets if sniper_wallets is not None else set(grad_positions.keys())
    sold_tokens_by_wallet: dict[str, float] = {}
    for s in sells:
        if s.signer in snipers:
            sold_tokens_by_wallet[s.signer] = sold_tokens_by_wallet.get(s.signer, 0.0) + s.token_amount

    sniper_grad_total = sum(
        pos for w, pos in grad_positions.items() if w in snipers and pos > 0
    )
    snipers_sold_pct: float | None = None
    if sniper_grad_total > 0:
        sold_total = sum(sold_tokens_by_wallet.values())
        snipers_sold_pct = round(min(sold_total / sniper_grad_total * 100, 100.0), 2)

    return SwapMetrics(
        team_buy_count=len(buys),
        team_sell_count=len(sells),
        team_net_sol=round(sell_sol - buy_sol, 4),
        snipers_sold_pct=snipers_sold_pct,
        coordinated_sell_count=detect_coordinated_sells(swaps),
    )


# ── IO ───────────────────────────────────────────────────────────────────────

async def fetch_team_swaps(
    client,
    token_mint: str,
    wallets: list[str] | None,
    since_ts: int,
) -> list[Swap]:
    """Fetch swaps for `token_mint` since graduation via Solana Tracker (by mint).

    `wallets` is unused for fetching now (one mint-level call returns all traders);
    the caller filters team-only afterward. Kept for signature compatibility.
    """
    all_swaps = await client.get_token_trades(token_mint, since_ts=since_ts)
    return dedup_swaps(filter_token_swaps(all_swaps, token_mint, since_ts))


# ── persistence ────────────────────────────────────────────────────────────────

def upsert_swaps(
    conn,
    token_mint: str,
    swaps: list[Swap],
    sniper_wallets: set[str],
    is_team: bool = True,
    team_wallets: set[str] | None = None,
    smart_money_wallets: set[str] | None = None,
) -> int:
    """Batch-upsert swaps into post_grad_swaps. Returns rows written.

    If team_wallets is given, is_team is set per-wallet (for the broadened top-20
    tracking); otherwise the scalar is_team applies to all rows.
    """
    if not swaps:
        return 0
    sm = smart_money_wallets or set()
    rows = [
        (
            token_mint,
            s.signer,
            s.side,
            s.sol_amount,
            s.token_amount,
            price_sol(s),
            s.timestamp,
            s.slot,
            1 if s.signer in sniper_wallets else 0,
            (1 if s.signer in team_wallets else 0) if team_wallets is not None else (1 if is_team else 0),
            1 if s.signer in sm else 0,
            s.tx_signature,
            s.price_usd,
        )
        for s in swaps
    ]
    conn.executemany(
        """INSERT INTO post_grad_swaps
               (token_mint, wallet_address, side, sol_amount, token_amount,
                price_sol, ts, slot, is_sniper, is_team, is_smart_money,
                tx_signature, price_usd)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(token_mint, wallet_address, slot, side) DO UPDATE SET
               sol_amount     = excluded.sol_amount,
               token_amount   = excluded.token_amount,
               price_sol      = excluded.price_sol,
               ts             = excluded.ts,
               is_smart_money = excluded.is_smart_money,
               tx_signature   = COALESCE(excluded.tx_signature, post_grad_swaps.tx_signature),
               price_usd      = COALESCE(excluded.price_usd, post_grad_swaps.price_usd)""",
        rows,
    )
    conn.commit()
    return len(rows)
