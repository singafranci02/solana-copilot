"""Phase C: cross-coin wallet behavioral fingerprints + similarity gate."""

import sqlite3
from pathlib import Path

import pytest

from src.analyzer.wallet_behavior import (
    compute_wallet_behavior, behavior_vector, update_wallet_behavior,
    load_behavior_vectors, _MIN_COINS_FOR_SIMILARITY,
)

_SCHEMA = (Path(__file__).parent.parent / "db" / "schema.sql").read_text()
W = "AVzP2GeRmqGphJsMxWoqjpUifPpCret7LqWhD8NWQK49"
W2 = "GugU1tP7doLeTw9hQP51xRJyS8Da1fWxuiy2rVrnMD2m"


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(_SCHEMA)
    yield c
    c.close()


def test_compute_aggregates_styles_and_sniper_rate():
    accum = [
        {"first_buy_offset_s": 5, "total_sol_in": 1.0, "accumulation_style": "sniped"},
        {"first_buy_offset_s": 10, "total_sol_in": 1.1, "accumulation_style": "sniped"},
        {"first_buy_offset_s": 400, "total_sol_in": 5.0, "accumulation_style": "gradual"},
    ]
    b = compute_wallet_behavior(accum, [], [], pnl_proxy=0.5, sig_count=4, wallet_age_days=1.0)
    assert b["n_coins_bc"] == 3
    assert b["pct_sniped"] == pytest.approx(2 / 3, abs=0.01)
    assert b["sniper_rate"] == pytest.approx(2 / 3, abs=0.01)   # 2 of 3 ≤120s
    assert b["avg_buy_size_sol"] == pytest.approx(2.3667, abs=0.01)
    assert b["cv_buy_size"] > 0


def test_behavior_vector_is_9d_clipped():
    v = behavior_vector({"sniper_rate": 1.0, "avg_first_buy_offset_s": 600,
                         "cv_buy_size": 5.0, "avg_buy_size_sol": 100})
    assert len(v) == 9
    assert all(0.0 <= x <= 1.0 for x in v)
    assert v[1] == 1.0   # 600/300 clipped to 1


def _seed_coin(conn, mint, wallet, style="sniped", offset=5, sol=1.0):
    conn.execute("INSERT OR IGNORE INTO tokens (mint,symbol,name,launchpad,created_at) "
                 "VALUES (?,?,?,'pump.fun',1)", (mint, "T", "T"))
    conn.execute("INSERT OR IGNORE INTO wallets (address) VALUES (?)", (wallet,))
    conn.execute("INSERT INTO bc_accumulation (token_mint,wallet_address,first_buy_offset_s,"
                 "bc_buy_count,bc_sell_count,total_sol_in,accumulation_style) "
                 "VALUES (?,?,?,?,?,?,?)", (mint, wallet, offset, 1, 0, sol, style))


def test_update_and_gate(conn):
    # wallet W across 3 coins → clears gate; W2 across 1 coin → excluded
    for i in range(3):
        _seed_coin(conn, f"m{i}" + "x" * 42, W)
    _seed_coin(conn, "only" + "y" * 40, W2)
    conn.commit()
    update_wallet_behavior([W, W2], conn)

    rw = conn.execute("SELECT n_coins_bc FROM wallet_behavior WHERE address=?", (W,)).fetchone()
    assert rw["n_coins_bc"] == 3
    vecs = load_behavior_vectors([W, W2], conn)
    assert W in vecs                       # n_coins_bc=3 >= gate
    assert W2 not in vecs                   # n_coins_bc=1 < gate
    assert _MIN_COINS_FOR_SIMILARITY == 3


def test_exit_one_shot_frac_and_hold_duration(conn):
    mint = "e" * 44
    _seed_coin(conn, mint, W, offset=5)
    conn.execute("INSERT INTO token_buyers (token_mint,wallet_address,bought_at,sol_amount,tokens_received) "
                 "VALUES (?,?,?,?,?)", (mint, W, 1000, 1.0, 1e6))
    # two sells: one big (one-shot-ish), later timestamp
    for ta, ts in [(800000.0, 1600), (200000.0, 1800)]:
        conn.execute("INSERT INTO post_grad_swaps (token_mint,wallet_address,side,sol_amount,"
                     "token_amount,ts,slot) VALUES (?,?,?,?,?,?,?)",
                     (mint, W, "sell", 1.0, ta, ts, ts))
    conn.commit()
    # add 2 more coins so it clears the gate for persistence
    for i in range(2):
        _seed_coin(conn, f"x{i}" + "z" * 42, W)
    conn.commit()
    update_wallet_behavior([W], conn)
    r = conn.execute("SELECT exit_one_shot_frac, avg_hold_duration_s, n_coins_exit "
                     "FROM wallet_behavior WHERE address=?", (W,)).fetchone()
    assert r["exit_one_shot_frac"] == pytest.approx(0.8, abs=0.01)   # 800k/1000k
    assert r["avg_hold_duration_s"] == pytest.approx(600, abs=1)     # 1600-1000
    assert r["n_coins_exit"] == 1
