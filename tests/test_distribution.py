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
