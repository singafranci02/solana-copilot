"""Phase 3: flow features, funding info parsing, creator reputation,
serial-deployer lookup, feature snapshot."""

import json
import sqlite3
from pathlib import Path

import pytest

from src.analyzer.flow_features import compute_bc_flow_features, _gini
from src.analyzer.smart_money import update_creator_reputation, get_creator_reputation
from src.analyzer.team_detect import get_past_deployments
from src.ingest.rpc import _funder_from_tx
from src.ingest.helius import Swap

_SCHEMA = (Path(__file__).parent.parent / "db" / "schema.sql").read_text()

W1 = "AVzP2GeRmqGphJsMxWoqjpUifPpCret7LqWhD8NWQK49"
W2 = "GugU1tP7doLeTw9hQP51xRJyS8Da1fWxuiy2rVrnMD2m"
CREATOR = "9DrvZvyWh1HuAoZxvYWMvkf2XCzryCpGgHqrMjyDWpmo"
MINT = "M" * 44


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(_SCHEMA)
    yield c
    c.close()


def _swap(wallet, ts, side="buy", sol=1.0):
    return Swap(side=side, token_mint=MINT, sol_amount=sol, token_amount=100.0,
                signer=wallet, timestamp=ts, slot=ts)


def test_bc_flow_features():
    t0 = 1_000
    swaps = [
        _swap(W1, t0 + 5, sol=10.0),          # first-60s buy
        _swap(W2, t0 + 5, sol=1.0),           # same second, distinct wallet → bundle
        _swap(W1, t0 + 200, sol=2.0),
        _swap(W2, t0 + 300, side="sell", sol=3.0),
    ]
    f = compute_bc_flow_features(swaps, t0)
    assert f.n_trades == 4
    assert f.n_buyers == 2
    assert f.n_sellers == 1
    assert f.buys_first_60s == 2
    assert f.same_second_bundle_count == 1
    assert f.sol_in == 13.0
    assert f.sol_out == 3.0
    assert f.top5_buyer_share == 1.0
    assert 0 < f.gini_buy_size < 1     # 12 vs 1 SOL → unequal


def test_gini_bounds():
    assert _gini([1.0, 1.0, 1.0]) == 0.0
    assert _gini([]) == 0.0
    assert _gini([5.0]) == 0.0
    # max Gini for n values is (n-1)/n → 0.5 at n=2, →1 as n grows
    assert _gini([0.01, 100.0]) == pytest.approx(0.5, abs=0.01)
    assert _gini([0.01] * 9 + [1000.0]) > 0.85


def test_funder_from_tx_transfer_checked_and_inner():
    tx = {
        "transaction": {"message": {"instructions": []}},
        "meta": {"innerInstructions": [{"instructions": [{
            "parsed": {"type": "transferChecked",
                       "info": {"destination": W1, "source": W2, "lamports": 5_000_000}},
        }]}]},
    }
    assert _funder_from_tx(tx, W1) == (W2, 5_000_000)
    # createAccount funding (newAccount instead of destination)
    tx2 = {"transaction": {"message": {"instructions": [{
        "parsed": {"type": "createAccount",
                   "info": {"newAccount": W1, "source": W2, "lamports": 1_000}},
    }]}}}
    assert _funder_from_tx(tx2, W1) == (W2, 1_000)
    assert _funder_from_tx({"transaction": {"message": {"instructions": []}}}, W1) is None


def test_creator_reputation_gates(conn):
    for i in range(7):
        update_creator_reputation(CREATOR, f"m{i}" + "x" * 40, "rug", conn)
    rep = get_creator_reputation(CREATOR, conn)
    assert rep["n"] == 7 and rep["rug_rate"] == 1.0
    assert rep["is_serial_rugger"] is False   # n<8 gate holds
    update_creator_reputation(CREATOR, "m7" + "x" * 40, "rug", conn)
    assert get_creator_reputation(CREATOR, conn)["is_serial_rugger"] is True
    assert get_creator_reputation(None, conn) is None


def test_get_past_deployments_sql(conn):
    for i, mint in enumerate(("a" * 44, "b" * 44)):
        conn.execute(
            "INSERT INTO tokens (mint, symbol, name, launchpad, created_at, creator_wallet) "
            "VALUES (?, 'T', 'T', 'pump.fun', ?, ?)", (mint, i, CREATOR),
        )
    conn.commit()
    assert get_past_deployments(CREATOR, conn) == ["a" * 44, "b" * 44]
    assert get_past_deployments(CREATOR) == []          # no conn → legacy behavior
    assert get_past_deployments("", conn) == []


def test_structural_read_serial_rugger_factor():
    from src.strategy.rules import structural_read
    read = structural_read({
        "creator_rep": {"n": 9, "rug_rate": 0.9, "is_serial_rugger": True},
    })
    assert any("serial rugger" in f for f in read.dominant_factors)


def test_feature_snapshot_written(conn):
    from src.ingest.graduation_monitor import _snapshot_features
    from src.common.models import GraduationEvent
    from src.strategy.rules import structural_read

    conn.execute(
        "INSERT INTO tokens (mint, symbol, name, launchpad, created_at) "
        "VALUES (?, 'T', 'T', 'pump.fun', 1)", (MINT,),
    )
    ctx = {"smart_money_count": 1, "top_holder_pct": 5.0, "unique_bc_buyers": 42}
    read = structural_read(ctx)
    event = GraduationEvent(token_mint=MINT, graduated_at=2,
                            detection_lag_seconds=0, pumpswap_pool_address=None,
                            bc_top_holders=[])
    _snapshot_features(event, ctx, read, conn)

    row = conn.execute("SELECT * FROM graduation_feature_snapshot").fetchone()
    feats = json.loads(row["features_json"])
    assert row["pipeline_version"] == 2
    assert feats["unique_bc_buyers"] == 42
    assert feats["verdict"] == read.verdict
