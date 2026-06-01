"""Tests for src/analyzer/bc_reconstruct.py — pure functions only (offline)."""

from src.ingest.helius import Swap
from src.analyzer.bc_reconstruct import (
    classify_accumulation,
    to_token_buyers,
)
from src.analyzer.post_grad_swaps import filter_token_swaps_window

MINT = "MINTaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def _swap(signer, side, sol, tokens, ts, slot, mint=MINT):
    return Swap(
        side=side, token_mint=mint, sol_amount=sol, token_amount=tokens,
        signer=signer, timestamp=ts, slot=slot,
    )


# ── classify_accumulation ──────────────────────────────────────────────────────

def test_sniped_single_buy_within_30s():
    created = 1000
    swaps = [_swap("W1", "buy", 5.0, 1000, 1010, 50)]  # +10s
    a = classify_accumulation(swaps, created)
    assert a.accumulation_style == "sniped"
    assert a.first_buy_offset_s == 10.0
    assert a.bc_buy_count == 1


def test_single_late_buy():
    created = 1000
    swaps = [_swap("W1", "buy", 5.0, 1000, 1200, 50)]  # +200s
    a = classify_accumulation(swaps, created)
    assert a.accumulation_style == "single"


def test_gradual_multiple_buys():
    created = 1000
    swaps = [
        _swap("W1", "buy", 2.0, 400, 1010, 50),
        _swap("W1", "buy", 3.0, 600, 1300, 51),
    ]
    a = classify_accumulation(swaps, created)
    assert a.accumulation_style == "gradual"
    assert a.bc_buy_count == 2
    assert a.total_sol_in == 5.0


def test_offset_clamped_non_negative():
    created = 2000
    swaps = [_swap("W1", "buy", 1.0, 100, 1990, 50)]  # before created (clock skew)
    a = classify_accumulation(swaps, created)
    assert a.first_buy_offset_s == 0.0


def test_sells_counted_separately():
    created = 1000
    swaps = [
        _swap("W1", "buy", 2.0, 400, 1010, 50),
        _swap("W1", "sell", 1.0, 200, 1500, 51),
    ]
    a = classify_accumulation(swaps, created)
    assert a.bc_buy_count == 1
    assert a.bc_sell_count == 1
    assert a.total_sol_in == 2.0  # sells don't count toward sol_in


# ── filter_token_swaps_window ──────────────────────────────────────────────────

def test_window_inclusive_bounds():
    swaps = [
        _swap("W1", "buy", 1.0, 100, 1000, 50),  # at lo
        _swap("W1", "buy", 1.0, 100, 1500, 51),  # mid
        _swap("W1", "buy", 1.0, 100, 2000, 52),  # at hi
        _swap("W1", "buy", 1.0, 100, 2001, 53),  # past hi
    ]
    out = filter_token_swaps_window(swaps, MINT, 1000, 2000)
    assert len(out) == 3


def test_window_drops_other_mint():
    swaps = [_swap("W1", "buy", 1.0, 100, 1500, 50, mint="OTHER")]
    assert filter_token_swaps_window(swaps, MINT, 1000, 2000) == []


# ── to_token_buyers ─────────────────────────────────────────────────────────────

def test_to_token_buyers_aggregates_per_wallet():
    swaps = [
        _swap("W1", "buy", 2.0, 400, 1010, 50),
        _swap("W1", "buy", 3.0, 600, 1300, 51),
        _swap("W2", "buy", 1.0, 100, 1050, 52),
        _swap("W1", "sell", 1.0, 200, 1500, 53),  # ignored
    ]
    buyers = sorted(to_token_buyers(swaps, MINT), key=lambda b: b.wallet_address)
    assert len(buyers) == 2
    w1 = buyers[0]
    assert w1.wallet_address == "W1"
    assert w1.sol_amount == 5.0
    assert w1.tokens_received == 1000
    assert w1.bought_at == 1010  # earliest buy


def test_to_token_buyers_empty_when_no_buys():
    swaps = [_swap("W1", "sell", 1.0, 200, 1500, 50)]
    assert to_token_buyers(swaps, MINT) == []
