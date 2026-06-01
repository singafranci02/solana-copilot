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


def filter_token_swaps(swaps: list[Swap], token_mint: str, since_ts: int) -> list[Swap]:
    """Keep only swaps for this token at or after the graduation timestamp."""
    return [s for s in swaps if s.token_mint == token_mint and s.timestamp >= since_ts]


def price_sol(swap: Swap) -> float | None:
    """SOL price per token for a swap, or None if token_amount is zero."""
    if swap.token_amount and swap.token_amount > 0:
        return swap.sol_amount / swap.token_amount
    return None


def detect_coordinated_sells(swaps: list[Swap], window_s: int = COORDINATED_WINDOW_S) -> int:
    """Count time windows in which ≥2 distinct wallets sold.

    Walks sells in timestamp order; opens a window at the first sell and counts
    one coordinated event if that window collects sells from ≥2 distinct wallets,
    then resumes after the window closes.
    """
    sells = sorted((s for s in swaps if s.side == "sell"), key=lambda s: s.timestamp)
    events = 0
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
            events += 1
        # advance past this window
        i = j
    return events


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
    helius,
    token_mint: str,
    wallets: list[str],
    since_ts: int,
) -> list[Swap]:
    """Fetch + parse all swaps for `wallets` on `token_mint` since graduation.

    Uses the passed-in HeliusClient so calls share its rate-limit semaphore.
    Re-fetches the full window each time; dedup_swaps + the table PK make it
    idempotent across the 1h/4h/24h checks.
    """
    async def _one(addr: str) -> list[Swap]:
        try:
            txs = await helius.get_transactions_for_address(addr, limit=100)
        except Exception as exc:
            logger.debug("tx fetch failed for %s..: %s", addr[:8], exc)
            return []
        out: list[Swap] = []
        for tx in txs or []:
            sw = parse_swap(tx)
            if sw is not None:
                out.append(sw)
        return out

    results = await asyncio.gather(*[_one(w) for w in wallets], return_exceptions=True)
    all_swaps: list[Swap] = []
    for r in results:
        if isinstance(r, list):
            all_swaps.extend(r)

    return dedup_swaps(filter_token_swaps(all_swaps, token_mint, since_ts))


# ── persistence ────────────────────────────────────────────────────────────────

def upsert_swaps(
    conn,
    token_mint: str,
    swaps: list[Swap],
    sniper_wallets: set[str],
    is_team: bool = True,
) -> int:
    """Batch-upsert swaps into post_grad_swaps. Returns rows written."""
    if not swaps:
        return 0
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
            1 if is_team else 0,
        )
        for s in swaps
    ]
    conn.executemany(
        """INSERT INTO post_grad_swaps
               (token_mint, wallet_address, side, sol_amount, token_amount,
                price_sol, ts, slot, is_sniper, is_team)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(token_mint, wallet_address, slot, side) DO UPDATE SET
               sol_amount   = excluded.sol_amount,
               token_amount = excluded.token_amount,
               price_sol    = excluded.price_sol,
               ts           = excluded.ts""",
        rows,
    )
    conn.commit()
    return len(rows)
