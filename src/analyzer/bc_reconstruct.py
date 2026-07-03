"""Bonding-curve accumulation reconstruction (pre-graduation behaviour).

The thesis is that HOW a team accumulates during the bonding curve predicts how
they behave post-graduation. We only had a holder snapshot AT graduation, never
the accumulation timeline. This reconstructs each top holder's BC buy/sell history
by reusing parse_swap + get_transactions_for_address (same engine as post_grad_swaps).

TIMING-CRITICAL: must run AT graduation. Helius get_transactions_for_address returns
only the recent ~100 txs per wallet; a token's BC trades are only still in that window
right after graduation. Backfilling old tokens yields sparse/incomplete data.
"""

import asyncio
import logging
from dataclasses import dataclass

from src.common.models import TokenBuyer
from src.ingest.helius import Swap
from src.analyzer.post_grad_swaps import filter_token_swaps_window, dedup_swaps

logger = logging.getLogger(__name__)

_SNIPER_THRESHOLD_S = 30.0   # matches team_detect.py sniper definition


@dataclass
class BcAccumulation:
    wallet: str
    first_buy_offset_s: float | None
    bc_buy_count: int
    bc_sell_count: int
    total_sol_in: float
    accumulation_style: str | None   # "sniped" | "gradual" | "single" | None


# ── pure functions ─────────────────────────────────────────────────────────────

def classify_accumulation(
    wallet_swaps: list[Swap],
    token_created_at: int,
) -> BcAccumulation:
    """Classify a single wallet's bonding-curve accumulation pattern.

    wallet_swaps: this wallet's swaps for the token, within the BC window.
    """
    wallet = wallet_swaps[0].signer if wallet_swaps else ""
    buys = [s for s in wallet_swaps if s.side == "buy"]
    sells = [s for s in wallet_swaps if s.side == "sell"]
    total_sol_in = round(sum(s.sol_amount for s in buys), 4)

    first_buy_offset: float | None = None
    if buys:
        first_buy_ts = min(s.timestamp for s in buys)
        first_buy_offset = float(max(0, first_buy_ts - token_created_at))

    style: str | None = None
    if len(buys) == 0:
        style = None
    elif len(buys) >= 2:
        style = "gradual"
    else:  # exactly one buy
        if first_buy_offset is not None and first_buy_offset <= _SNIPER_THRESHOLD_S:
            style = "sniped"
        else:
            style = "single"

    return BcAccumulation(
        wallet=wallet,
        first_buy_offset_s=first_buy_offset,
        bc_buy_count=len(buys),
        bc_sell_count=len(sells),
        total_sol_in=total_sol_in,
        accumulation_style=style,
    )


def to_token_buyers(swaps: list[Swap], token_mint: str) -> list[TokenBuyer]:
    """Collapse each wallet's BC buys into a TokenBuyer for the token_buyers backfill."""
    by_wallet: dict[str, list[Swap]] = {}
    for s in swaps:
        if s.side == "buy":
            by_wallet.setdefault(s.signer, []).append(s)

    out: list[TokenBuyer] = []
    for wallet, buys in by_wallet.items():
        out.append(TokenBuyer(
            token_mint=token_mint,
            wallet_address=wallet,
            bought_at=min(s.timestamp for s in buys),
            sol_amount=round(sum(s.sol_amount for s in buys), 6),
            tokens_received=round(sum(s.token_amount for s in buys), 6),
        ))
    return out


# ── IO ───────────────────────────────────────────────────────────────────────

async def reconstruct_bc_holders(
    client,
    token_mint: str,
    holder_wallets: list[str] | None,
    token_created_at: int,
    graduated_at: int,
    structural: frozenset[str] = frozenset(),
) -> tuple[dict[str, BcAccumulation], list[Swap]]:
    """Reconstruct BC accumulation from the token's trade history (Solana Tracker).

    One mint-level call returns ALL bonding-curve traders (not just top holders),
    filtered to the BC window [token_created_at, graduated_at]. `holder_wallets` is
    no longer needed for fetching. Returns (per-wallet BcAccumulation, BC swaps).

    `structural` drops swaps signed by pool/curve/program accounts (e.g. the
    migration transaction) so they never become token_buyers or bundle members.
    """
    raw = await client.get_token_trades(
        token_mint, since_ts=token_created_at, until_ts=graduated_at, sort="ASC",
    )
    bc_swaps = dedup_swaps(
        filter_token_swaps_window(raw, token_mint, token_created_at, graduated_at)
    )
    if structural:
        bc_swaps = [s for s in bc_swaps if s.signer not in structural]

    by_wallet: dict[str, list[Swap]] = {}
    for s in bc_swaps:
        by_wallet.setdefault(s.signer, []).append(s)

    profiles = {
        wallet: classify_accumulation(ws, token_created_at)
        for wallet, ws in by_wallet.items()
    }
    return profiles, bc_swaps


# ── persistence ────────────────────────────────────────────────────────────────

def upsert_bc_accumulation(conn, token_mint: str, profiles: dict[str, BcAccumulation]) -> int:
    """Batch-upsert BC accumulation profiles. Returns rows written."""
    if not profiles:
        return 0
    rows = [
        (
            token_mint, p.wallet, p.first_buy_offset_s,
            p.bc_buy_count, p.bc_sell_count, p.total_sol_in, p.accumulation_style,
        )
        for p in profiles.values()
    ]
    conn.executemany(
        """INSERT INTO bc_accumulation
               (token_mint, wallet_address, first_buy_offset_s,
                bc_buy_count, bc_sell_count, total_sol_in, accumulation_style)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(token_mint, wallet_address) DO UPDATE SET
               first_buy_offset_s = excluded.first_buy_offset_s,
               bc_buy_count       = excluded.bc_buy_count,
               bc_sell_count      = excluded.bc_sell_count,
               total_sol_in       = excluded.total_sol_in,
               accumulation_style = excluded.accumulation_style""",
        rows,
    )
    conn.commit()
    return len(rows)
