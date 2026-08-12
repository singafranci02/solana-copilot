"""Follow-return measurement — the fill rules are the whole point of it."""

import sqlite3

from src.analyzer.follow_return import (
    FEE, TAKE_PROFIT, _fill, follow_returns_for_coin,
)


def _db(rows):
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE post_grad_swaps (token_mint TEXT, wallet_address TEXT,
                 ts INT, side TEXT, price_usd REAL, is_team INT)""")
    c.executemany("INSERT INTO post_grad_swaps VALUES ('M',?,?,?,?,?)", rows)
    return c


def test_fill_medians_the_window_rather_than_taking_one_print():
    """A single bad print must not set the fill — that artifact once made a
    take-profit backtest read a mean of 31.95x."""
    prints = [(100, 1.0), (105, 1.0), (108, 50.0)]      # 50.0 is the bad tick
    assert _fill(prints, 100) == 1.0


def test_fill_falls_forward_when_the_window_is_empty():
    assert _fill([(100, 2.0), (500, 3.0)], 200) == 3.0


def test_take_profit_needs_a_sustained_level():
    """One print at the target is not a fillable level."""
    base = [("w", 10, "buy", 1.0, 1)] + [("x", 10 + i, "buy", 1.0, 0) for i in range(40)]
    # single print far above target, plus a normal print after the deadline so the
    # position has something to exit at (no exit price = the trade is not measurable)
    spike = base + [("x", 200, "buy", 99.0, 0), ("x", 620, "buy", 1.0, 0)]
    out = follow_returns_for_coin(_db(spike), "M")
    assert out, "expected a team buy to be measured"
    assert out[0][2] < 1 + TAKE_PROFIT, "single spike must not fill the take-profit"


def test_fees_are_deducted():
    rows = [("w", 10, "buy", 1.0, 1)] + [("x", 10 + i, "buy", 1.0, 0) for i in range(40)]
    rows += [("x", 300 + i, "buy", 2.0, 0) for i in range(5)]   # sustained above target
    out = follow_returns_for_coin(_db(rows), "M")
    assert abs(out[0][2] - (1 + TAKE_PROFIT) * (1 - FEE)) < 1e-9


def test_thin_tapes_are_refused():
    """Under 30 prints there is no basis for pricing a fill."""
    assert follow_returns_for_coin(_db([("w", 10, "buy", 1.0, 1)]), "M") == []


def test_only_team_buys_are_measured():
    rows = [("w", 10, "sell", 1.0, 1)] + [("x", 10 + i, "buy", 1.0, 0) for i in range(40)]
    assert follow_returns_for_coin(_db(rows), "M") == []
