"""Cross-reference token buyers against the smart money wallet database.

Scoring formula (all factors in [0, 1]):
  60% — 90-day win rate           (from GMGN)
  25% — trade volume signal        (log-scaled, saturates at 500 trades)
  15% — recency of last activity   (decays to 0 after 90 days idle in our DB)

The score is written back to wallets.smart_money_score on every call so the
DB always reflects the freshest calculation.
"""

import sqlite3
import time
from typing import TYPE_CHECKING

from src.common.models import TokenBuyer, Wallet

if TYPE_CHECKING:
    from src.ingest.gmgn import GMGNClient

SMART_MONEY_THRESHOLD = 0.7  # minimum score to be considered smart money


# ── DB helpers ────────────────────────────────────────────────────────────────

def _row_to_wallet(row: sqlite3.Row) -> Wallet:
    return Wallet(
        address=row["address"],
        label=row["label"],
        smart_money_score=float(row["smart_money_score"]),
        win_rate_90d=row["win_rate_90d"],
        total_trades=int(row["total_trades"] or 0),
        first_seen=row["first_seen"],
        funding_source=row["funding_source"],
    )


def upsert_wallet(wallet: Wallet, conn: sqlite3.Connection) -> None:
    """Insert or update a wallet row; preserves funding_source if already set."""
    conn.execute(
        """
        INSERT INTO wallets
            (address, label, smart_money_score, win_rate_90d,
             total_trades, first_seen, funding_source)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(address) DO UPDATE SET
            label             = excluded.label,
            smart_money_score = excluded.smart_money_score,
            win_rate_90d      = excluded.win_rate_90d,
            total_trades      = excluded.total_trades,
            first_seen        = excluded.first_seen,
            funding_source    = COALESCE(wallets.funding_source, excluded.funding_source)
        """,
        (
            wallet.address,
            wallet.label,
            wallet.smart_money_score,
            wallet.win_rate_90d,
            wallet.total_trades,
            wallet.first_seen,
            wallet.funding_source,
        ),
    )
    conn.commit()


# ── public API ────────────────────────────────────────────────────────────────

def get_smart_money_wallets(conn: sqlite3.Connection) -> list[Wallet]:
    """Fetch all wallets with smart_money_score >= 0.7, ordered best-first.

    Args:
        conn: Active SQLite connection.

    Returns:
        List of Wallet instances considered smart money.
    """
    rows = conn.execute(
        """
        SELECT address, label, smart_money_score, win_rate_90d,
               total_trades, first_seen, funding_source
        FROM wallets
        WHERE smart_money_score >= ?
        ORDER BY smart_money_score DESC
        """,
        (SMART_MONEY_THRESHOLD,),
    ).fetchall()
    return [_row_to_wallet(r) for r in rows]


def score_wallet(wallet: Wallet, conn: sqlite3.Connection) -> float:
    """Compute a smart money score and write it back to wallets.smart_money_score.

    Args:
        wallet: Wallet to score (win_rate_90d and total_trades used directly).
        conn: Active SQLite connection (queried for recency, updated with score).

    Returns:
        Score in [0.0, 1.0].
    """
    # Factor 1: 90-day win rate (primary signal)
    wr = float(wallet.win_rate_90d or 0.0)

    # Factor 2: trade volume — more trades → signal is more reliable
    volume_signal = min(wallet.total_trades / 500.0, 1.0)

    # Factor 3: recency — when did this wallet last appear in our token_buyers?
    row = conn.execute(
        "SELECT MAX(bought_at) FROM token_buyers WHERE wallet_address = ?",
        (wallet.address,),
    ).fetchone()
    last_ts = (row[0] or 0) if row else 0
    days_idle = (time.time() - last_ts) / 86_400.0 if last_ts else 90.0
    recency = max(0.0, 1.0 - days_idle / 90.0)

    raw_score = 0.60 * wr + 0.25 * volume_signal + 0.15 * recency
    score = round(max(0.0, min(1.0, raw_score)), 4)

    conn.execute(
        "UPDATE wallets SET smart_money_score = ? WHERE address = ?",
        (score, wallet.address),
    )
    conn.commit()

    return score


def find_smart_money_in_buyers(
    buyers: list[TokenBuyer],
    smart_money: list[Wallet],
) -> list[Wallet]:
    """Return the smart_money wallets that appear in buyers.

    Args:
        buyers: All known buyers for a token.
        smart_money: Pre-fetched smart money wallet list.

    Returns:
        Smart money wallets that bought this token, preserving score order.
    """
    buyer_addresses = {b.wallet_address for b in buyers}
    return [w for w in smart_money if w.address in buyer_addresses]


async def enrich_wallet(
    address: str,
    gmgn: "GMGNClient",
    conn: sqlite3.Connection,
) -> Wallet:
    """Fetch GMGN profile, compute score, and upsert the wallet into the DB.

    This is the primary async entry point that wires GMGN data into the
    scoring + persistence pipeline.

    Args:
        address: Solana wallet address.
        gmgn: Authenticated GMGNClient instance.
        conn: Active SQLite connection.

    Returns:
        Wallet with smart_money_score populated and written to the DB.
    """
    from src.ingest.gmgn import parse_wallet_profile  # avoid circular at module level

    raw = await gmgn.get_wallet_profile(address)
    wallet = parse_wallet_profile(address, raw)

    # Ensure the row exists before score_wallet tries to UPDATE it
    upsert_wallet(wallet, conn)

    wallet.smart_money_score = score_wallet(wallet, conn)
    # score_wallet writes the score; sync the in-memory object
    return wallet
