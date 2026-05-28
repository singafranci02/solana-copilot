"""Cross-reference token buyers against the smart money wallet database.

Scoring formula (all factors in [0, 1]):
  60% — 90-day win rate           (from local coin_outcomes — self-learning)
  25% — trade volume signal        (log-scaled, saturates at 500 trades)
  15% — recency of last activity   (decays to 0 after 90 days idle in our DB)

The score is written back to wallets.smart_money_score on every call so the
DB always reflects the freshest calculation.

Minimum sample sizes (enforced in code, not schema):
  wallet win_rate   — requires total_calls >= 15 in wallet_stats
  funder_reputation — requires len(graduated_mints) >= 8 for is_known_rugger
"""

import json
import sqlite3
import time
from typing import TYPE_CHECKING

from src.common.models import FunderReputation, TokenBuyer, Wallet, WalletStats

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


# ── Graduation-context: incremental wallet_stats ──────────────────────────────

_MIN_WALLET_SAMPLE = 15   # minimum calls before win_rate is considered significant


def update_wallet_stats(
    address: str,
    outcome: str,       # "moon" | "ok" | "rug"
    is_graduated: bool,
    conn: sqlite3.Connection,
) -> None:
    """Increment wallet_stats counters after a 4h outcome is recorded.

    Never fully recomputes — only increments counters.
    win_rate is set to NULL until total_calls >= 15.
    """
    conn.execute(
        """INSERT INTO wallet_stats (address, graduated_calls, wins, losses, total_calls, last_updated)
           VALUES (?, ?, ?, ?, 1, ?)
           ON CONFLICT(address) DO UPDATE SET
               graduated_calls = graduated_calls + excluded.graduated_calls,
               wins            = wins  + excluded.wins,
               losses          = losses + excluded.losses,
               total_calls     = total_calls + 1,
               last_updated    = excluded.last_updated""",
        (
            address,
            1 if is_graduated else 0,
            1 if outcome == "moon" else 0,
            1 if outcome == "rug" else 0,
            int(time.time()),
        ),
    )
    # Recompute win_rate only when sample is sufficient
    conn.execute(
        """UPDATE wallet_stats
           SET win_rate = CASE
               WHEN total_calls >= ? THEN CAST(wins AS REAL) / MAX(wins + losses, 1)
               ELSE NULL
           END
           WHERE address = ?""",
        (_MIN_WALLET_SAMPLE, address),
    )
    conn.commit()


def get_wallet_stats(
    address: str, conn: sqlite3.Connection
) -> WalletStats | None:
    """Return wallet_stats for address, or None if not found."""
    row = conn.execute(
        "SELECT * FROM wallet_stats WHERE address = ?", (address,)
    ).fetchone()
    if row is None:
        return None
    return WalletStats(
        address=row["address"],
        graduated_calls=int(row["graduated_calls"] or 0),
        wins=int(row["wins"] or 0),
        losses=int(row["losses"] or 0),
        total_calls=int(row["total_calls"] or 0),
        win_rate=row["win_rate"],
        last_updated=int(row["last_updated"] or 0),
    )


# ── Graduation-context: funder_reputation ────────────────────────────────────

_MIN_FUNDER_SAMPLE = 8    # minimum graduated mints before is_known_rugger can be set
_KNOWN_RUGGER_THRESHOLD = 0.65


def update_funder_reputation(
    funding_source: str,
    token_mint: str,
    outcome: str,         # "moon" | "ok" | "rug"
    bundle_pct: float,
    dev_pct: float,
    conn: sqlite3.Connection,
) -> None:
    """Incrementally update funder_reputation after a 4h outcome is known."""
    existing = conn.execute(
        "SELECT * FROM funder_reputation WHERE funding_source = ?", (funding_source,)
    ).fetchone()

    now = int(time.time())

    if existing is None:
        mints = [token_mint]
        rug_count = 1 if outcome == "rug" else 0
        moon_count = 1 if outcome == "moon" else 0
        ok_count = 1 if outcome == "ok" else 0
        n = 1
        rug_rate = rug_count / n
        is_rugger = 0
        conn.execute(
            """INSERT INTO funder_reputation
               (funding_source, graduated_mints, rug_count, moon_count, ok_count,
                rug_rate, avg_bundle_pct, avg_dev_pct, last_seen, is_known_rugger)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                funding_source, json.dumps(mints),
                rug_count, moon_count, ok_count,
                rug_rate, bundle_pct, dev_pct, now, is_rugger,
            ),
        )
    else:
        mints = json.loads(existing["graduated_mints"])
        if token_mint not in mints:
            mints.append(token_mint)
        rug_count = int(existing["rug_count"]) + (1 if outcome == "rug" else 0)
        moon_count = int(existing["moon_count"]) + (1 if outcome == "moon" else 0)
        ok_count = int(existing["ok_count"]) + (1 if outcome == "ok" else 0)
        n = len(mints)
        rug_rate = rug_count / n
        # Rolling average for bundle and dev pct
        prev_n = n - 1 or 1
        avg_bundle = (float(existing["avg_bundle_pct"]) * prev_n + bundle_pct) / n
        avg_dev = (float(existing["avg_dev_pct"]) * prev_n + dev_pct) / n
        is_rugger = int(n >= _MIN_FUNDER_SAMPLE and rug_rate >= _KNOWN_RUGGER_THRESHOLD)
        conn.execute(
            """UPDATE funder_reputation SET
               graduated_mints = ?, rug_count = ?, moon_count = ?, ok_count = ?,
               rug_rate = ?, avg_bundle_pct = ?, avg_dev_pct = ?,
               last_seen = ?, is_known_rugger = ?
               WHERE funding_source = ?""",
            (
                json.dumps(mints), rug_count, moon_count, ok_count,
                rug_rate, round(avg_bundle, 2), round(avg_dev, 2),
                now, is_rugger, funding_source,
            ),
        )
    conn.commit()


def get_funder_reputation(
    funding_source: str, conn: sqlite3.Connection
) -> FunderReputation | None:
    """Return the FunderReputation for a known funder, or None.

    Note: is_known_rugger is only True when len(graduated_mints) >= 8
    and rug_rate >= 0.65. Below that threshold the data is treated as
    insufficient — patterns.py returns it as 'hypothesis, insufficient data'.
    """
    row = conn.execute(
        "SELECT * FROM funder_reputation WHERE funding_source = ?", (funding_source,)
    ).fetchone()
    if row is None:
        return None
    mints = json.loads(row["graduated_mints"])
    n = len(mints) or 1
    return FunderReputation(
        funding_source=row["funding_source"],
        graduated_mints=mints,
        rug_count=int(row["rug_count"]),
        moon_count=int(row["moon_count"]),
        ok_count=int(row["ok_count"]),
        rug_rate=float(row["rug_rate"]),
        moon_rate=int(row["moon_count"]) / n,
        avg_bundle_pct=float(row["avg_bundle_pct"]),
        avg_dev_pct=float(row["avg_dev_pct"]),
        last_seen=row["last_seen"],
        is_known_rugger=bool(row["is_known_rugger"]),
    )
