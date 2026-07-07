"""Structural-account filtering — the pool/curve/program exclusion layer."""

from src.analyzer.structural_accounts import (
    PUMP_FUN_TOTAL_SUPPLY,
    STATIC_STRUCTURAL,
    extract_pool_accounts,
    extract_total_supply,
    filter_holders,
    structural_set,
)
from src.ingest.graduation_monitor import _parse_bc_holders
from src.analyzer.team_detect import build_team_cluster_post_grad
from src.common.models import TokenBuyer

POOL_ID = "8sLbNZoA1cfnvMJLPfp98ZLAnFSYCFApfJKMbiXNLwxj"
RAYDIUM_AUTH = "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1"
WALLET_A = "AVzP2GeRmqGphJsMxWoqjpUifPpCret7LqWhD8NWQK49"
WALLET_B = "GugU1tP7doLeTw9hQP51xRJyS8Da1fWxuiy2rVrnMD2m"

TOKEN_INFO = {
    "token": {"name": "T", "symbol": "T"},
    "pools": [
        {"poolId": POOL_ID, "tokenSupply": 1_000_000_000, "market": "pump-amm"},
        {"poolId": "short", "tokenSupply": 0},   # malformed entries tolerated
        "not-a-dict",
    ],
}


def test_extract_pool_accounts_shape_tolerant():
    assert extract_pool_accounts(TOKEN_INFO) == {POOL_ID}
    assert extract_pool_accounts(None) == set()
    assert extract_pool_accounts({"pools": None}) == set()


def test_extract_total_supply():
    assert extract_total_supply(TOKEN_INFO) == 1_000_000_000
    assert extract_total_supply(None) == PUMP_FUN_TOTAL_SUPPLY
    assert extract_total_supply({"pools": [{"tokenSupply": "bad"}]}) == PUMP_FUN_TOTAL_SUPPLY


def test_structural_set_unions_all_layers():
    s = structural_set(TOKEN_INFO, cex_addresses={WALLET_B}, extra={"X" * 32})
    assert POOL_ID in s
    assert WALLET_B in s
    assert "X" * 32 in s
    assert STATIC_STRUCTURAL <= s


def test_filter_holders_drops_pool_and_authority():
    accounts = [
        {"address": POOL_ID, "uiAmount": 800_000_000.0},       # the AMM pool
        {"address": RAYDIUM_AUTH, "uiAmount": 50_000_000.0},   # static structural
        {"address": WALLET_A, "uiAmount": 30_000_000.0},
    ]
    out = filter_holders(accounts, structural_set(TOKEN_INFO))
    assert [a["address"] for a in out] == [WALLET_A]


def test_parse_bc_holders_uses_real_supply_denominator():
    # Without the real supply, one whale among the returned top-N reads as ~100%.
    accounts = [{"address": WALLET_A, "uiAmount": 30_000_000.0}]
    holders = _parse_bc_holders(accounts, total_supply=1_000_000_000.0)
    assert holders[0]["pct"] == 3.0
    # Legacy fallback (no supply) still works
    assert _parse_bc_holders(accounts)[0]["pct"] == 100.0


def test_team_cluster_fallback_excludes_structural():
    # No buyer overlap → top-5 fallback. The pool must NOT land in the team.
    holders = [
        {"wallet": POOL_ID, "pct": 63.8, "ui_amount": 638_000_000.0},
        {"wallet": WALLET_A, "pct": 5.0, "ui_amount": 50_000_000.0},
        {"wallet": WALLET_B, "pct": 3.0, "ui_amount": 30_000_000.0},
    ]
    tc, _scored = build_team_cluster_post_grad(
        "M" * 44, [], holders, frozenset(),
        structural_addresses=frozenset({POOL_ID}),
    )
    assert tc is not None
    assert POOL_ID not in tc.member_addresses
    assert tc.supply_pct_at_graduation == 8.0


def test_team_cluster_overlap_branch_excludes_structural():
    holders = [
        {"wallet": POOL_ID, "pct": 63.8, "ui_amount": 638_000_000.0},
        {"wallet": WALLET_A, "pct": 5.0, "ui_amount": 50_000_000.0},
    ]
    buyers = [
        TokenBuyer(token_mint="M" * 44, wallet_address=POOL_ID,
                   sol_amount=10.0, tokens_received=1.0, bought_at=100),
        TokenBuyer(token_mint="M" * 44, wallet_address=WALLET_A,
                   sol_amount=1.0, tokens_received=1.0, bought_at=100),
    ]
    tc, _scored = build_team_cluster_post_grad(
        "M" * 44, buyers, holders, frozenset(),
        structural_addresses=frozenset({POOL_ID}),
    )
    assert tc is not None
    assert tc.member_addresses == [WALLET_A]


def test_extract_market_state():
    from src.analyzer.structural_accounts import extract_market_state
    raw = {
        "holders": 423,
        "pools": [
            {"liquidity": {"usd": 5000.0}, "marketCap": {"usd": 12000.0},
             "price": {"usd": 0.00004}, "txns": {"buys": 10, "sells": 4, "total": 14}},
            {"liquidity": {"usd": 60000.0}, "marketCap": {"usd": 69000.0},
             "price": {"usd": 0.00006}, "txns": {"buys": 100, "sells": 40, "total": 140}},
        ],
    }
    m = extract_market_state(raw)
    assert m["holder_count"] == 423
    assert m["liquidity_usd"] == 60000.0        # highest-liquidity pool wins
    assert m["market_cap_usd"] == 69000.0
    assert m["txns_total"] == 140
    # tolerant of junk
    empty = extract_market_state(None)
    assert empty["holder_count"] is None and empty["liquidity_usd"] is None
    assert extract_market_state({"pools": []})["market_cap_usd"] is None
