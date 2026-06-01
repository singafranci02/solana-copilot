"""Shared persistence for the live_trades order-flow table.

Used by both the historical backfill (scripts/backfill_live_trades.py) and the
live watcher (src/services/live_watcher.py). live_trades rows are immutable facts,
so writes are INSERT OR IGNORE on a synthetic dedup_key.
"""

from dataclasses import dataclass


@dataclass
class LiveTrade:
    token_mint: str
    wallet_address: str
    side: str                 # "buy" | "sell"
    sol_amount: float
    token_amount: float
    ts: int
    slot: int | None = None
    signature: str | None = None
    price_usd: float | None = None
    wallet_tag: str = "unknown"
    source: str = "live"      # "live" | "backfill"


def dedup_key(t: LiveTrade) -> str:
    """Stable key for idempotent inserts.

    Prefer the on-chain signature when present; otherwise a composite that is
    collision-free in practice for record-only purposes.
    """
    if t.signature:
        return f"{t.signature}:{t.wallet_address}"
    return f"{t.token_mint}:{t.wallet_address}:{t.ts}:{t.side}:{round(t.sol_amount, 6)}"


def price_sol(t: LiveTrade) -> float | None:
    if t.token_amount and t.token_amount > 0:
        return t.sol_amount / t.token_amount
    return None


def insert_trades(conn, trades: list[LiveTrade]) -> int:
    """Batch INSERT OR IGNORE live trades. Returns the number attempted."""
    if not trades:
        return 0
    rows = [
        (
            t.token_mint, t.wallet_address, t.side, t.sol_amount, t.token_amount,
            price_sol(t), t.price_usd, t.ts, t.slot, t.signature,
            t.source, t.wallet_tag, dedup_key(t),
        )
        for t in trades
    ]
    conn.executemany(
        """INSERT OR IGNORE INTO live_trades
               (token_mint, wallet_address, side, sol_amount, token_amount,
                price_sol, price_usd, ts, slot, signature, source, wallet_tag, dedup_key)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    conn.commit()
    return len(rows)
