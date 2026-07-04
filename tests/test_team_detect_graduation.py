"""Tests for build_team_cluster_post_grad in src/analyzer/team_detect.py."""

import pytest

from src.analyzer.team_detect import build_team_cluster_post_grad
from src.common.models import TeamCluster, TokenBuyer

MINT = "MINTaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
CEX_ADDR = "H8sMJSCQxfKiFTCfDR3DUMLPwcRbM61LGFJ8N4dK3WjS"

_CEX: frozenset[str] = frozenset([CEX_ADDR])
_NO_CEX: frozenset[str] = frozenset()


def _buyer(wallet: str, bought_at: int = 1000, sol: float = 1.0, tokens: float = 1e6) -> TokenBuyer:
    return TokenBuyer(
        token_mint=MINT,
        wallet_address=wallet,
        bought_at=bought_at,
        sol_amount=sol,
        tokens_received=tokens,
    )


def _holder(wallet: str, pct: float) -> dict:
    return {"wallet": wallet, "pct": pct, "ui_amount": pct * 1e9}


# ── Basic functionality ────────────────────────────────────────────────────────

def test_returns_none_when_no_eligible_holders():
    """All holders are CEX → no cluster."""
    holders = [_holder(CEX_ADDR, 20.0)]
    result, _scored = build_team_cluster_post_grad(MINT, [], holders, _CEX)
    assert result is None


def test_returns_none_when_holders_list_empty():
    buyers = [_buyer("wallet_a")]
    result, _scored = build_team_cluster_post_grad(MINT, buyers, [], _NO_CEX)
    assert result is None


def test_overlap_buyers_and_holders_preferred():
    """Wallets that both bought in BC and hold at graduation should be candidates."""
    w_bc_holder = "wallet_bc_holder"
    w_holder_only = "wallet_holder_only"
    buyers = [_buyer(w_bc_holder), _buyer("wallet_other")]
    holders = [
        _holder(w_bc_holder, 30.0),
        _holder(w_holder_only, 20.0),
    ]
    result, _scored = build_team_cluster_post_grad(MINT, buyers, holders, _NO_CEX)
    assert result is not None
    assert w_bc_holder in result.member_addresses
    # wallet_holder_only did NOT buy in BC, so should not be in the overlap
    # (may still appear if fallback used — but overlap case should prefer BC buyers)
    assert result.token_mint == MINT


def test_falls_back_to_top_holders_when_no_overlap():
    """If no buyers match top holders, top-5 holders are used as candidates."""
    w_holder = "wallet_holder_only"
    buyers = [_buyer("wallet_other")]
    holders = [_holder(w_holder, 25.0)]
    result, _scored = build_team_cluster_post_grad(MINT, buyers, holders, _NO_CEX)
    assert result is not None
    assert w_holder in result.member_addresses


def test_supply_pct_computed_from_holders():
    w1 = "wallet_one"
    w2 = "wallet_two"
    buyers = [_buyer(w1), _buyer(w2)]
    holders = [_holder(w1, 15.0), _holder(w2, 10.0)]
    result, _scored = build_team_cluster_post_grad(MINT, buyers, holders, _NO_CEX)
    assert result is not None
    assert abs(result.supply_pct_at_graduation - 25.0) < 0.01


def test_cex_wallets_excluded_from_candidates():
    buyers = [_buyer(CEX_ADDR), _buyer("wallet_legit")]
    holders = [_holder(CEX_ADDR, 50.0), _holder("wallet_legit", 5.0)]
    result, _scored = build_team_cluster_post_grad(MINT, buyers, holders, _CEX)
    assert result is not None
    assert CEX_ADDR not in result.member_addresses


# ── Sniper detection ──────────────────────────────────────────────────────────

def test_bc_sniper_detected_when_first_buy_within_30s():
    launch_ts = 1_000_000
    w_sniper = "wallet_sniper"
    buyers = [
        _buyer("wallet_normal", bought_at=launch_ts),   # first observed buy = launch
        _buyer(w_sniper, bought_at=launch_ts + 10),     # 10s after → sniper
    ]
    holders = [_holder(w_sniper, 20.0)]
    result, _scored = build_team_cluster_post_grad(MINT, buyers, holders, _NO_CEX)
    assert result is not None
    # offset is relative to min bought_at across all buyers
    assert result.first_buy_offset_seconds <= 30.0
    assert result.is_bc_sniper is True


def test_not_sniper_when_first_buy_after_30s():
    launch_ts = 1_000_000
    w_late = "wallet_late"
    buyers = [
        _buyer("wallet_first", bought_at=launch_ts),
        _buyer(w_late, bought_at=launch_ts + 120),   # 2 min after
    ]
    holders = [_holder(w_late, 20.0)]
    result, _scored = build_team_cluster_post_grad(MINT, buyers, holders, _NO_CEX)
    assert result is not None
    assert result.is_bc_sniper is False


def test_cluster_id_is_unique_across_calls():
    buyers = [_buyer("wallet_a")]
    holders = [_holder("wallet_a", 20.0)]
    r1, _scored = build_team_cluster_post_grad(MINT, buyers, holders, _NO_CEX)
    r2, _scored = build_team_cluster_post_grad(MINT, buyers, holders, _NO_CEX)
    assert r1 is not None and r2 is not None
    assert r1.cluster_id != r2.cluster_id


# ── Realistic graduated token scenario ────────────────────────────────────────

def test_realistic_dump_setup():
    """Simulate a classic dump setup: team bought BC early, still holds >50%."""
    launch_ts = 2_000_000
    team_wallets = [f"team_{i}" for i in range(5)]
    other_wallets = [f"retail_{i}" for i in range(10)]

    buyers = (
        [_buyer(w, bought_at=launch_ts + i * 5, tokens=5e6) for i, w in enumerate(team_wallets)]
        + [_buyer(w, bought_at=launch_ts + 300 + i * 30, tokens=1e6) for i, w in enumerate(other_wallets)]
    )
    holders = (
        [_holder(w, 10.5) for w in team_wallets]   # team holds 52.5% total
        + [_holder(w, 0.8) for w in other_wallets]
    )
    result, _scored = build_team_cluster_post_grad(MINT, buyers, holders, _NO_CEX)
    assert result is not None
    assert result.supply_pct_at_graduation > 40.0   # team holds majority
    assert result.is_bc_sniper is True               # bought within 30s of first buyer
