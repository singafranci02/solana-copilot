"""Phase D: exit choreography — order, leader, spread, funder rollup."""

import sqlite3
from pathlib import Path

import pytest

from src.analyzer.exit_choreography import (
    compute_exit_choreography, upsert_team_member_behavior, update_funder_choreography,
)
from src.analyzer.post_grad_swaps import coordinated_sell_windows, detect_coordinated_sells
from src.ingest.helius import Swap

_SCHEMA = (Path(__file__).parent.parent / "db" / "schema.sql").read_text()
MINT = "M" * 44
A = "AVzP2GeRmqGphJsMxWoqjpUifPpCret7LqWhD8NWQK49"
B = "GugU1tP7doLeTw9hQP51xRJyS8Da1fWxuiy2rVrnMD2m"
C = "7s1da8DduuBFqGra5bJBjpnvL5E9mGzCuMk1Qkh4or2Z"
FUNDER = "ECHhYtSogLASVDZ8NTg1w7oCo2aeJGnNu4pDNvorwB9a"


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(_SCHEMA)
    yield c
    c.close()


def _sell(w, ts, tokens):
    return Swap(side="sell", token_mint=MINT, sol_amount=1.0, token_amount=tokens,
               signer=w, timestamp=ts, slot=ts)


def test_exit_order_leader_and_spread():
    grad = 1000
    swaps = [_sell(B, 1100, 5e5), _sell(A, 1050, 5e5), _sell(C, 1400, 5e5)]
    grad_positions = {A: 1e6, B: 1e6, C: 1e6}
    choreo = compute_exit_choreography(swaps, grad_positions, grad, {A, B, C})
    assert choreo.leader_wallet == A          # sold first (t=1050)
    order = {m.wallet: m.exit_order for m in choreo.members}
    assert order == {A: 1, B: 2, C: 3}
    assert choreo.exit_spread_s == 350        # 1400 - 1050
    a = next(m for m in choreo.members if m.wallet == A)
    assert a.is_first_seller and a.first_sell_offset_s == 50
    assert a.sold_pct == 50.0                 # sold 5e5 of 1e6


def test_coordinated_windows_membership():
    swaps = [_sell(A, 100, 1), _sell(B, 120, 1), _sell(C, 5000, 1)]
    windows = coordinated_sell_windows(swaps, window_s=300)
    assert windows == [{A, B}]                # A,B within 300s; C alone
    assert detect_coordinated_sells(swaps, 300) == 1


def test_non_seller_member_has_null_order():
    choreo = compute_exit_choreography(
        [_sell(A, 1100, 1e6)], {A: 1e6, B: 1e6}, 1000, {A, B},
    )
    b = next(m for m in choreo.members if m.wallet == B)
    assert b.exit_order is None and b.first_sell_offset_s is None
    assert b.sold_pct == 0.0        # held full position → sold 0%, not null


def test_upsert_and_funder_leader_consistency(conn):
    conn.execute("INSERT INTO tokens (mint,symbol,name,launchpad,created_at) "
                 "VALUES (?,?,?,'pump.fun',1)", (MINT, "T", "T"))
    conn.commit()
    choreo = compute_exit_choreography(
        [_sell(A, 1050, 5e5), _sell(B, 1100, 5e5)], {A: 1e6, B: 1e6}, 1000, {A, B},
    )
    upsert_team_member_behavior(conn, MINT, choreo, offset_h=4)
    row = conn.execute("SELECT exit_order, sold_pct_4h, is_first_seller "
                       "FROM team_member_behavior WHERE wallet=?", (A,)).fetchone()
    assert row["exit_order"] == 1 and row["is_first_seller"] == 1
    assert row["sold_pct_4h"] == 50.0

    # funder rollup: A leads twice → consistency climbs; unrelated leader once → dips
    update_funder_choreography(conn, FUNDER, choreo)
    update_funder_choreography(conn, FUNDER, choreo)
    fp = conn.execute("SELECT leader_wallet, leader_consistency, choreography_sample_count "
                      "FROM team_fingerprints WHERE funding_source=?", (FUNDER,)).fetchone()
    assert fp["leader_wallet"] == A
    assert fp["choreography_sample_count"] == 2
    assert fp["leader_consistency"] == 1.0
