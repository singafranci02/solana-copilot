"""Tests for src/analyzer/distribution.py — classification logic only.

All Helius network calls and DB writes are mocked so tests run offline.
"""

import pytest

from src.analyzer.distribution import _classify
from src.common.models import DistributionSignal


# ── _classify unit tests ──────────────────────────────────────────────────────

def test_dumped_when_holder_count_below_threshold():
    signal = _classify(team_sold_pct=5.0, holder_count=3)
    assert signal == DistributionSignal.DUMPED


def test_dumped_takes_priority_over_sold_pct():
    """Even with no selling, if holders < 5 → DUMPED."""
    signal = _classify(team_sold_pct=0.0, holder_count=2)
    assert signal == DistributionSignal.DUMPED


def test_distributing_when_sold_over_30_pct():
    signal = _classify(team_sold_pct=35.0, holder_count=50)
    assert signal == DistributionSignal.DISTRIBUTING


def test_distributing_exactly_at_threshold():
    signal = _classify(team_sold_pct=30.0, holder_count=50)
    assert signal == DistributionSignal.DISTRIBUTING


def test_holding_when_minimal_movement():
    signal = _classify(team_sold_pct=5.0, holder_count=100)
    assert signal == DistributionSignal.HOLDING


def test_holding_when_team_sold_pct_is_none():
    signal = _classify(team_sold_pct=None, holder_count=100)
    assert signal == DistributionSignal.HOLDING


def test_accumulating_when_position_grew():
    """Negative sold_pct means the team bought more — ACCUMULATING."""
    signal = _classify(team_sold_pct=-15.0, holder_count=100)
    assert signal == DistributionSignal.ACCUMULATING


def test_accumulating_threshold_is_10_pct():
    # -9% growth is not enough to trigger ACCUMULATING
    assert _classify(team_sold_pct=-9.0, holder_count=100) == DistributionSignal.HOLDING
    # -11% is
    assert _classify(team_sold_pct=-11.0, holder_count=100) == DistributionSignal.ACCUMULATING


def test_holding_just_below_distributing():
    signal = _classify(team_sold_pct=29.9, holder_count=50)
    assert signal == DistributionSignal.HOLDING


# ── Signal enum consistency ────────────────────────────────────────────────────

def test_signal_values_are_strings():
    """DistributionSignal extends str, so .value comparisons work."""
    assert DistributionSignal.DUMPED.value == "DUMPED"
    assert DistributionSignal.DISTRIBUTING.value == "DISTRIBUTING"
    assert DistributionSignal.HOLDING.value == "HOLDING"
    assert DistributionSignal.ACCUMULATING.value == "ACCUMULATING"


def test_all_signal_variants_covered():
    covered = {
        _classify(team_sold_pct=None, holder_count=100),
        _classify(team_sold_pct=-15.0, holder_count=100),
        _classify(team_sold_pct=10.0, holder_count=100),
        _classify(team_sold_pct=35.0, holder_count=100),
        _classify(team_sold_pct=0.0, holder_count=2),
    }
    assert covered == set(DistributionSignal)


# ── tape flag re-stamping ─────────────────────────────────────────────────────
# The 1h/4h/24h passes used to refetch the whole tape from graduation and re-upsert
# it, which is how is_team/is_sniper/is_smart_money got their final values. That
# refetch cost ~137 API calls per coin, so it was replaced by an in-place SQL stamp.
# trajectory.py derives the team-exit label from is_team, so these flags must end up
# identical to what the old full re-upsert produced.

import sqlite3

from src.analyzer.distribution import _apply_tape_flags


def _tape_db(rows):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE post_grad_swaps (
               token_mint TEXT, wallet_address TEXT, ts INTEGER,
               is_team INTEGER DEFAULT 0, is_sniper INTEGER DEFAULT 0,
               is_smart_money INTEGER DEFAULT 0)"""
    )
    conn.executemany(
        "INSERT INTO post_grad_swaps (token_mint, wallet_address, ts) VALUES (?,?,?)", rows
    )
    return conn


def _flags(conn, wallet):
    r = conn.execute(
        "SELECT is_team, is_sniper, is_smart_money FROM post_grad_swaps "
        "WHERE wallet_address = ? LIMIT 1", (wallet,)).fetchone()
    return (r["is_team"], r["is_sniper"], r["is_smart_money"])


def test_flags_are_stamped_across_rows_written_by_earlier_checkpoints():
    conn = _tape_db([("M", "team1", 10), ("M", "team1", 20), ("M", "rando", 30)])
    _apply_tape_flags(conn, "M", {"team1"}, set(), set())
    assert conn.execute(
        "SELECT COUNT(*) FROM post_grad_swaps WHERE wallet_address='team1' AND is_team=1"
    ).fetchone()[0] == 2
    assert _flags(conn, "rando") == (0, 0, 0)


def test_stale_flags_are_cleared_when_membership_changes():
    """A wallet dropped from the team must not keep a stale is_team=1 — the exit
    label would then be timed off a non-member's sell."""
    conn = _tape_db([("M", "ex_member", 10)])
    _apply_tape_flags(conn, "M", {"ex_member"}, set(), set())
    assert _flags(conn, "ex_member")[0] == 1
    _apply_tape_flags(conn, "M", set(), set(), set())
    assert _flags(conn, "ex_member")[0] == 0


def test_sniper_and_smart_money_are_independent():
    conn = _tape_db([("M", "w", 10)])
    _apply_tape_flags(conn, "M", {"w"}, {"w"}, {"w"})
    assert _flags(conn, "w") == (1, 1, 1)


def test_other_mints_are_untouched():
    conn = _tape_db([("M", "w", 10), ("OTHER", "w", 10)])
    _apply_tape_flags(conn, "M", {"w"}, set(), set())
    other = conn.execute(
        "SELECT is_team FROM post_grad_swaps WHERE token_mint='OTHER'").fetchone()["is_team"]
    assert other == 0


def test_wallets_absent_from_the_tape_cost_nothing():
    """smart_money is a global set; only tape participants may be stamped."""
    conn = _tape_db([("M", "w", 10)])
    _apply_tape_flags(conn, "M", set(), set(), {f"sm{i}" for i in range(5000)})
    assert conn.execute(
        "SELECT COUNT(*) FROM post_grad_swaps WHERE is_smart_money=1").fetchone()[0] == 0
