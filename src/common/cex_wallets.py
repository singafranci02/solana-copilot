"""CEX hot wallet registry for Solana.

Seed addresses are hardcoded here; the cex_hotwallets DB table extends
this list over time as new addresses are confirmed via Solscan / Arkham.

TODO: run a periodic job to pull fresh labels from Solscan's label API
      and Arkham Intelligence to expand and verify this list.
"""

import sqlite3
import time

# ── Hardcoded seed addresses ──────────────────────────────────────────────────
# Format: (address, exchange, label)
# Sourced from public Solscan labels and community research.
# All marked confirmed=0 until independently verified on-chain.
_SEED: list[tuple[str, str, str]] = [
    # Binance
    ("2ojv9BAiHUrvsm9gxDe7fJSzbNZSJcxZvf8dqmWGHG8", "binance", "Binance Hot 1"),
    ("5tzFkiKscXHK5ZXCGbGuYth6sDPhHDnD6yA7pxGQtEMM", "binance", "Binance Hot 2"),
    ("9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM", "binance", "Binance Deposit"),
    ("HVh6wHNBAsG3pq1Bj5oCzRjoWKVogEDHwUHkRz3ekFgt", "binance", "Binance Hot 3"),
    # Coinbase
    ("H8sMJSCQxfKiFTCfDR3DUMLPwcRbM61LGFJ8N4dK3WjS", "coinbase", "Coinbase Prime"),
    ("GJRs4FwHtemZ5ZE9x3FNvJ8TMwitKTh21yxdRPqn7as5", "coinbase", "Coinbase Hot"),
    ("A7JahHFmmNAaHyE4LGGnZLHG5bPqUDMuXeNpbcwdNMxS", "coinbase", "Coinbase Deposit"),
    # OKX
    ("5VCwKtCXgCJ6kit5FybXjvriW3xELsFDhYrPSqtJNmcD", "okx", "OKX Hot"),
    ("AobVSwdW7eqxCFhRhSQLPnzGDMMrKKSg7MkxnFZDqiJW", "okx", "OKX Deposit"),
    # KuCoin
    ("BmFdpraQhkiDQE6SnfG5omcA1VwzqfXrwtNYBwWTymy6", "kucoin", "KuCoin Hot"),
    # Bybit
    ("FWznbcNXWQuHTawe9RxvQ2LdCENssh12pkR9Z7j7pPf6", "bybit", "Bybit Hot"),
    # Gate.io
    ("HjSCCG3FPWjCr9Yqu4z5DGtbCvPpUpMaAXMmHPsKJFNe", "gate", "Gate.io Hot"),
    # MEXC
    ("MEXCtBNFkBnfMHMRVEWEkTj12cpyNdCYkfKvKAFW36s", "mexc", "MEXC Hot"),
    # Kraken
    ("FYa25XnBsXQHMgPdto8hhBrCmRmBJgdHGV7cELK1Jdav", "kraken", "Kraken Hot"),
]

_SEED_ADDRESSES: frozenset[str] = frozenset(a for a, _, _ in _SEED)


def is_cex_wallet(address: str, conn: sqlite3.Connection | None = None) -> bool:
    """Return True if address is a known CEX hot wallet.

    Checks the hardcoded seed first (O(1)), then the DB extended list.
    Pass conn=None to skip the DB check — safe for hot paths before DB open.
    """
    if address in _SEED_ADDRESSES:
        return True
    if conn is None:
        return False
    try:
        row = conn.execute(
            "SELECT 1 FROM cex_hotwallets WHERE address = ?", (address,)
        ).fetchone()
        return row is not None
    except sqlite3.OperationalError:
        return False


def seed_cex_table(conn: sqlite3.Connection) -> None:
    """Insert seed addresses into cex_hotwallets if not already present."""
    now = int(time.time())
    for address, exchange, label in _SEED:
        conn.execute(
            """INSERT INTO cex_hotwallets (address, exchange, label, confirmed, added_at)
               VALUES (?, ?, ?, 0, ?)
               ON CONFLICT(address) DO NOTHING""",
            (address, exchange, label, now),
        )
    conn.commit()


def get_all_cex_addresses(conn: sqlite3.Connection) -> frozenset[str]:
    """Return union of seed + DB-extended CEX addresses."""
    try:
        rows = conn.execute("SELECT address FROM cex_hotwallets").fetchall()
        db_addrs = frozenset(r[0] for r in rows)
    except sqlite3.OperationalError:
        db_addrs = frozenset()
    return _SEED_ADDRESSES | db_addrs
