"""Phase 2 self-learning wiring: wallet_stats increments, fingerprint upserts,
proven-wallet verdict factor."""

import json
import sqlite3
from pathlib import Path

import pytest

from src.analyzer.outcome_tracker import _update_wallet_stats_for_buyers
from src.analyzer.team_memory import update_fingerprint, update_fingerprint_outcome
from src.analyzer.smart_money import update_wallet_stats
from src.common.models import TeamCluster
from src.strategy.rules import structural_read

_SCHEMA = (Path(__file__).parent.parent / "db" / "schema.sql").read_text()

W1 = "AVzP2GeRmqGphJsMxWoqjpUifPpCret7LqWhD8NWQK49"
W2 = "GugU1tP7doLeTw9hQP51xRJyS8Da1fWxuiy2rVrnMD2m"
MINT = "M" * 44


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(_SCHEMA)
    yield c
    c.close()


def _seed_buyers(conn, mint=MINT, wallets=(W1, W2)):
    conn.execute(
        "INSERT INTO tokens (mint, symbol, name, launchpad, created_at) "
        "VALUES (?, 'T', 'T', 'pump.fun', 1)", (mint,),
    )
    for w in wallets:
        conn.execute("INSERT OR IGNORE INTO wallets (address) VALUES (?)", (w,))
        conn.execute(
            "INSERT INTO token_buyers (token_mint, wallet_address, bought_at, "
            "sol_amount, tokens_received) VALUES (?, ?, 1, 1.0, 1.0)", (mint, w),
        )
    conn.commit()


def test_wallet_stats_incremented_for_all_buyers(conn):
    _seed_buyers(conn)
    conn.execute(
        "INSERT INTO graduation_events (token_mint, graduated_at) VALUES (?, 2)",
        (MINT,),
    )
    conn.commit()
    _update_wallet_stats_for_buyers(MINT, "moon", conn)

    rows = conn.execute(
        "SELECT address, wins, total_calls, graduated_calls FROM wallet_stats"
    ).fetchall()
    assert {r["address"] for r in rows} == {W1, W2}
    for r in rows:
        assert (r["wins"], r["total_calls"], r["graduated_calls"]) == (1, 1, 1)


def test_wallet_stats_noop_without_classification(conn):
    _seed_buyers(conn)
    _update_wallet_stats_for_buyers(MINT, None, conn)
    assert conn.execute("SELECT COUNT(*) FROM wallet_stats").fetchone()[0] == 0


def test_win_rate_null_until_min_sample(conn):
    conn.execute("INSERT INTO wallets (address) VALUES (?)", (W1,))
    for _ in range(14):
        update_wallet_stats(W1, "moon", True, conn)
    assert conn.execute(
        "SELECT win_rate FROM wallet_stats WHERE address = ?", (W1,)
    ).fetchone()[0] is None
    update_wallet_stats(W1, "moon", True, conn)
    assert conn.execute(
        "SELECT win_rate FROM wallet_stats WHERE address = ?", (W1,)
    ).fetchone()[0] == 1.0


def test_fingerprint_writers_upsert_in_either_order(conn):
    """Both writers own disjoint columns of one row keyed by funding_source."""
    _seed_buyers(conn)
    conn.execute("UPDATE wallets SET funding_source = 'F' WHERE address = ?", (W1,))
    conn.execute(
        "INSERT INTO coin_outcomes (token_mint, check_offset_h, checked_at, classified) "
        "VALUES (?, 4, 3, 'rug')", (MINT,),
    )
    conn.commit()

    tc = TeamCluster(
        cluster_id="c1", token_mint=MINT, funding_source="F",
        member_addresses=[W1, W2], supply_pct_at_graduation=25.0,
        first_buy_offset_seconds=10.0, is_bc_sniper=True,
    )
    # structural writer first (previously crashed: no fingerprint_id, bad ON CONFLICT)
    update_fingerprint(tc, outcome="rug", conn=conn)
    update_fingerprint_outcome(MINT, conn)

    rows = conn.execute("SELECT * FROM team_fingerprints").fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["sample_count"] == 1
    assert row["rug_rate"] == 1.0
    assert json.loads(row["known_mints"]) == [MINT]
    assert row["avg_sniper_rate"] == 1.0


def test_structural_read_proven_wallet_factor():
    read = structural_read({"proven_wallet_count": 2})
    assert any("proven wallets" in f for f in read.dominant_factors)
    read_none = structural_read({"proven_wallet_count": 1})
    assert not any("proven wallets" in f for f in read_none.dominant_factors)
