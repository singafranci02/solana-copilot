"""Journal service — auto-ingest my own trades and expose tagging endpoints."""

import json
import sqlite3
from typing import Any

from src.common.models import Trade

_ALLOWED_TAG_COLS: frozenset[str] = frozenset({
    "exit_reason", "notes", "conviction", "rules_followed", "source_tag",
})


def save_trade(trade: Trade, conn: sqlite3.Connection) -> None:
    """Upsert a Trade record into the my_trades table.

    On conflict (same tx_signature), updates market-context columns but leaves
    user-supplied tags (conviction, notes, rules_followed, exit_reason) intact.
    """
    conn.execute(
        """
        INSERT INTO my_trades
            (tx_signature, token_mint, side, ts, sol_amount, tokens, price_sol,
             mc_at_entry, holders_at_entry, smart_money_in_count_at_entry,
             lp_burned, top10_pct, bundle_pct, dev_pct, source_tag,
             conviction, rules_followed, exit_reason, notes)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(tx_signature) DO UPDATE SET
            mc_at_entry                    = excluded.mc_at_entry,
            holders_at_entry               = excluded.holders_at_entry,
            smart_money_in_count_at_entry  = excluded.smart_money_in_count_at_entry,
            lp_burned                      = excluded.lp_burned,
            top10_pct                      = excluded.top10_pct,
            bundle_pct                     = excluded.bundle_pct,
            dev_pct                        = excluded.dev_pct
        """,
        (
            trade.tx_signature,
            trade.token_mint,
            trade.side,
            trade.ts,
            trade.sol_amount,
            trade.tokens,
            trade.price_sol,
            trade.mc_at_entry,
            trade.holders_at_entry,
            trade.smart_money_in_count_at_entry,
            int(trade.lp_burned) if trade.lp_burned is not None else None,
            trade.top10_pct,
            trade.bundle_pct,
            trade.dev_pct,
            trade.source_tag,
            trade.conviction,
            json.dumps(trade.rules_followed),
            trade.exit_reason,
            trade.notes,
        ),
    )
    conn.commit()


def update_trade_tags(
    tx_signature: str,
    updates: dict[str, Any],
    conn: sqlite3.Connection,
) -> None:
    """Apply partial updates (conviction, notes, exit_reason, etc.) to a trade.

    Only columns in _ALLOWED_TAG_COLS may be updated to prevent SQL injection
    from untrusted API payloads.
    """
    safe = {k: v for k, v in updates.items() if k in _ALLOWED_TAG_COLS}
    if not safe:
        return
    set_clause = ", ".join(f"{col} = ?" for col in safe)
    conn.execute(
        f"UPDATE my_trades SET {set_clause} WHERE tx_signature = ?",  # noqa: S608
        [*safe.values(), tx_signature],
    )
    conn.commit()


def compute_trade_pnl(tx_signature: str, conn: sqlite3.Connection) -> float | None:
    """Calculate realised PnL in SOL for a buy/sell pair.

    For a buy leg: finds the earliest matching sell and returns (sell_sol - buy_sol).
    For a sell leg: finds the latest preceding buy and returns (sell_sol - buy_sol).
    Returns None if the paired leg doesn't exist yet.
    """
    row = conn.execute(
        "SELECT token_mint, side, sol_amount, ts FROM my_trades WHERE tx_signature = ?",
        (tx_signature,),
    ).fetchone()
    if row is None:
        return None

    mint, side, sol_amount, ts = row["token_mint"], row["side"], row["sol_amount"], row["ts"]

    if side == "buy":
        paired = conn.execute(
            """SELECT sol_amount FROM my_trades
               WHERE token_mint = ? AND side = 'sell' AND ts > ?
               ORDER BY ts ASC LIMIT 1""",
            (mint, ts),
        ).fetchone()
    else:
        paired = conn.execute(
            """SELECT sol_amount FROM my_trades
               WHERE token_mint = ? AND side = 'buy' AND ts < ?
               ORDER BY ts DESC LIMIT 1""",
            (mint, ts),
        ).fetchone()

    if paired is None:
        return None

    buy_sol = sol_amount if side == "buy" else paired["sol_amount"]
    sell_sol = paired["sol_amount"] if side == "buy" else sol_amount
    return sell_sol - buy_sol


async def ingest_tx(tx_signature: str, my_wallet: str) -> Trade | None:
    """Fetch, decode, and persist a single transaction from my wallet.

    Returns None if the transaction is not a swap by my_wallet.
    """
    from src.common.db import get_connection
    from src.ingest.helius import HeliusClient, parse_swap

    async with HeliusClient() as helius:
        tx = await helius.get_transaction(tx_signature)

    if not tx:
        return None

    swap = parse_swap(tx)
    if swap is None or swap.signer.lower() != my_wallet.lower():
        return None

    price_sol = swap.sol_amount / swap.token_amount if swap.token_amount else 0.0
    trade = Trade(
        tx_signature=tx_signature,
        token_mint=swap.token_mint,
        side=swap.side,
        ts=swap.timestamp,
        sol_amount=swap.sol_amount,
        tokens=swap.token_amount,
        price_sol=price_sol,
        source_tag="helius_ingest",
    )

    conn = get_connection()
    try:
        save_trade(trade, conn)
    finally:
        conn.close()

    return trade
