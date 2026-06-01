"""Tests for src/analyzer/post_grad_swaps.py — pure functions only (offline)."""

from src.ingest.helius import Swap
from src.analyzer.post_grad_swaps import (
    dedup_swaps,
    filter_token_swaps,
    price_sol,
    detect_coordinated_sells,
    compute_metrics,
)

MINT = "MINTaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
OTHER = "OTHERbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


def _swap(signer, side, sol, tokens, ts, slot, mint=MINT):
    return Swap(
        side=side, token_mint=mint, sol_amount=sol, token_amount=tokens,
        signer=signer, timestamp=ts, slot=slot,
    )


# ── dedup_swaps ────────────────────────────────────────────────────────────────

def test_dedup_collapses_same_key():
    s = _swap("W1", "sell", 1.0, 100, 1000, 50)
    dup = _swap("W1", "sell", 1.0, 100, 1000, 50)
    assert len(dedup_swaps([s, dup])) == 1


def test_dedup_keeps_opposite_side_same_slot():
    buy = _swap("W1", "buy", 1.0, 100, 1000, 50)
    sell = _swap("W1", "sell", 1.0, 100, 1000, 50)
    assert len(dedup_swaps([buy, sell])) == 2


def test_dedup_keeps_distinct_wallets():
    a = _swap("W1", "sell", 1.0, 100, 1000, 50)
    b = _swap("W2", "sell", 1.0, 100, 1000, 50)
    assert len(dedup_swaps([a, b])) == 2


# ── filter_token_swaps ─────────────────────────────────────────────────────────

def test_filter_drops_other_mint():
    keep = _swap("W1", "buy", 1.0, 100, 2000, 50)
    drop = _swap("W1", "buy", 1.0, 100, 2000, 51, mint=OTHER)
    out = filter_token_swaps([keep, drop], MINT, since_ts=1000)
    assert out == [keep]


def test_filter_drops_before_graduation():
    before = _swap("W1", "buy", 1.0, 100, 500, 50)
    after = _swap("W1", "buy", 1.0, 100, 1500, 51)
    out = filter_token_swaps([before, after], MINT, since_ts=1000)
    assert out == [after]


def test_filter_keeps_exactly_at_graduation():
    at = _swap("W1", "buy", 1.0, 100, 1000, 50)
    assert filter_token_swaps([at], MINT, since_ts=1000) == [at]


# ── price_sol ──────────────────────────────────────────────────────────────────

def test_price_sol_normal():
    assert price_sol(_swap("W1", "buy", 2.0, 100, 1000, 50)) == 0.02


def test_price_sol_zero_tokens_returns_none():
    assert price_sol(_swap("W1", "sell", 1.0, 0, 1000, 50)) is None


# ── detect_coordinated_sells ───────────────────────────────────────────────────

def test_coordinated_two_wallets_in_window():
    swaps = [
        _swap("W1", "sell", 1.0, 100, 1000, 50),
        _swap("W2", "sell", 1.0, 100, 1100, 51),  # 100s later, distinct wallet
    ]
    assert detect_coordinated_sells(swaps, window_s=300) == 1


def test_coordinated_same_wallet_twice_is_zero():
    swaps = [
        _swap("W1", "sell", 1.0, 100, 1000, 50),
        _swap("W1", "sell", 1.0, 100, 1100, 51),
    ]
    assert detect_coordinated_sells(swaps, window_s=300) == 0


def test_coordinated_outside_window_is_zero():
    swaps = [
        _swap("W1", "sell", 1.0, 100, 1000, 50),
        _swap("W2", "sell", 1.0, 100, 1400, 51),  # 400s > 300 window
    ]
    assert detect_coordinated_sells(swaps, window_s=300) == 0


def test_coordinated_boundary_exactly_window():
    swaps = [
        _swap("W1", "sell", 1.0, 100, 1000, 50),
        _swap("W2", "sell", 1.0, 100, 1300, 51),  # exactly 300s — inclusive
    ]
    assert detect_coordinated_sells(swaps, window_s=300) == 1


def test_coordinated_ignores_buys():
    swaps = [
        _swap("W1", "buy", 1.0, 100, 1000, 50),
        _swap("W2", "buy", 1.0, 100, 1100, 51),
    ]
    assert detect_coordinated_sells(swaps, window_s=300) == 0


# ── compute_metrics ─────────────────────────────────────────────────────────────

def test_metrics_counts_and_net_sol():
    swaps = [
        _swap("W1", "buy", 2.0, 100, 1000, 50),
        _swap("W1", "sell", 5.0, 100, 1100, 51),
        _swap("W2", "sell", 3.0, 50, 1200, 52),
    ]
    m = compute_metrics(swaps, grad_positions={}, sniper_wallets=set())
    assert m.team_buy_count == 1
    assert m.team_sell_count == 2
    # net = sell_sol(5+3) − buy_sol(2) = 6.0
    assert m.team_net_sol == 6.0


def test_metrics_snipers_sold_pct():
    # W1 held 1000 at graduation, sold 400 → 40%
    swaps = [_swap("W1", "sell", 1.0, 400, 1000, 50)]
    m = compute_metrics(swaps, grad_positions={"W1": 1000.0}, sniper_wallets={"W1"})
    assert m.snipers_sold_pct == 40.0


def test_metrics_snipers_sold_pct_capped_at_100():
    swaps = [
        _swap("W1", "sell", 1.0, 800, 1000, 50),
        _swap("W1", "sell", 1.0, 800, 1100, 51),  # sold more than held (rebought between)
    ]
    m = compute_metrics(swaps, grad_positions={"W1": 1000.0}, sniper_wallets={"W1"})
    assert m.snipers_sold_pct == 100.0


def test_metrics_snipers_sold_pct_none_without_positions():
    swaps = [_swap("W1", "sell", 1.0, 400, 1000, 50)]
    m = compute_metrics(swaps, grad_positions={}, sniper_wallets={"W1"})
    assert m.snipers_sold_pct is None
