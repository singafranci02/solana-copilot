"""Tests for src/strategy/rules.py — structural_read graduation verdict."""

import pytest

from src.common.models import FunderReputation, StructuralRead, TeamCluster
from src.strategy.rules import structural_read

MINT_A = "MINTaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def _team(supply_pct: float = 20.0, is_sniper: bool = False) -> TeamCluster:
    return TeamCluster(
        cluster_id="cluster-1",
        token_mint=MINT_A,
        supply_pct_at_graduation=supply_pct,
        is_bc_sniper=is_sniper,
        member_addresses=["wallet1", "wallet2"],
    )


def _funder(rug_rate: float = 0.0, n_mints: int = 10, is_rugger: bool = False) -> FunderReputation:
    return FunderReputation(
        funding_source="funder_abc",
        graduated_mints=["m" * 44] * n_mints,
        rug_rate=rug_rate,
        moon_rate=max(0, 1.0 - rug_rate - 0.1),
        rug_count=int(rug_rate * n_mints),
        moon_count=int((1 - rug_rate) * n_mints),
        is_known_rugger=is_rugger,
    )


# ── Hard SKIP conditions ───────────────────────────────────────────────────────

def test_known_rugger_is_skip():
    ctx = {"funder_rep": _funder(rug_rate=0.8, n_mints=10, is_rugger=True)}
    read = structural_read(ctx)
    assert read.verdict == "SKIP"
    assert read.funder_is_known_rugger is True
    assert read.confidence >= 0.85


def test_dumped_signal_is_skip():
    ctx = {"distribution_signal": "DUMPED"}
    read = structural_read(ctx)
    assert read.verdict == "SKIP"
    assert read.confidence >= 0.90


def test_high_supply_sniper_is_skip():
    ctx = {"team_cluster": _team(supply_pct=55.0, is_sniper=True)}
    read = structural_read(ctx)
    assert read.verdict == "SKIP"
    assert read.bundle_pct == 55.0


def test_high_supply_non_sniper_is_not_skip():
    """High supply alone without sniper flag should not be a hard skip."""
    ctx = {"team_cluster": _team(supply_pct=55.0, is_sniper=False)}
    read = structural_read(ctx)
    assert read.verdict != "SKIP"


# ── STRUCTURALLY_SOUND conditions ─────────────────────────────────────────────

def test_two_smart_money_holding_is_sound():
    ctx = {
        "smart_money_count": 2,
        "distribution_signal": "HOLDING",
        "team_cluster": _team(supply_pct=15.0),
    }
    read = structural_read(ctx)
    assert read.verdict == "STRUCTURALLY_SOUND"
    assert read.smart_money_count == 2


def test_accumulating_with_sm_is_sound():
    ctx = {
        "smart_money_count": 3,
        "distribution_signal": "ACCUMULATING",
    }
    read = structural_read(ctx)
    assert read.verdict == "STRUCTURALLY_SOUND"


def test_structurally_sound_not_triggered_when_distributing():
    """Even with smart money, DISTRIBUTING should prevent STRUCTURALLY_SOUND."""
    ctx = {
        "smart_money_count": 3,
        "distribution_signal": "DISTRIBUTING",
    }
    read = structural_read(ctx)
    assert read.verdict != "STRUCTURALLY_SOUND"


# ── WATCH conditions ──────────────────────────────────────────────────────────

def test_no_signals_is_watch():
    read = structural_read({})
    assert read.verdict == "WATCH"
    assert read.confidence == 0.50


def test_one_sm_no_distribution_is_watch():
    ctx = {"smart_money_count": 1}
    read = structural_read(ctx)
    assert read.verdict == "WATCH"


def test_watch_returns_what_would_change():
    read = structural_read({})
    assert len(read.what_would_change) > 10


# ── Output structure ──────────────────────────────────────────────────────────

def test_result_always_has_dominant_factors():
    """dominant_factors should never be empty."""
    for ctx in [{}, {"smart_money_count": 5}, {"distribution_signal": "DUMPED"}]:
        read = structural_read(ctx)
        assert len(read.dominant_factors) >= 1


def test_confidence_is_bounded():
    ctx = {"smart_money_count": 100, "distribution_signal": "ACCUMULATING"}
    read = structural_read(ctx)
    assert 0.0 <= read.confidence <= 1.0


def test_funder_with_good_track_record_boosts_score():
    good_funder = FunderReputation(
        funding_source="good_funder",
        graduated_mints=["m" * 44] * 10,
        rug_rate=0.1,
        moon_rate=0.6,
        moon_count=6,
        rug_count=1,
        ok_count=3,
        is_known_rugger=False,
    )
    ctx = {
        "smart_money_count": 2,
        "distribution_signal": "HOLDING",
        "funder_rep": good_funder,
    }
    read = structural_read(ctx)
    assert read.verdict == "STRUCTURALLY_SOUND"


def test_distributing_causes_skip_or_watch():
    ctx = {"distribution_signal": "DISTRIBUTING", "smart_money_count": 0}
    read = structural_read(ctx)
    assert read.verdict in ("SKIP", "WATCH")


def test_structural_read_is_pure():
    """Calling twice with same context must produce same result."""
    ctx = {"smart_money_count": 2, "distribution_signal": "HOLDING"}
    r1 = structural_read(ctx)
    r2 = structural_read(ctx)
    assert r1.verdict == r2.verdict
    assert r1.confidence == r2.confidence
