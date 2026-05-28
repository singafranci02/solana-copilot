"""Tests for src/analyzer/patterns.py — sample-size enforcement and query structure.

Uses an in-memory SQLite database seeded with enough rows to test both the
significant (n>=30) and insignificant (n<30) paths.
"""

import sqlite3
import time
import uuid
from pathlib import Path

import pytest

from src.analyzer.patterns import (
    MIN_SIGNIFICANT_N,
    PatternResult,
    avg_detection_lag_seconds,
    distribution_signal_vs_outcome,
    moon_rate_by_smart_money_count,
    rug_rate_by_sniper_flag,
    rug_rate_by_team_supply_pct,
)

SCHEMA_SQL = Path(__file__).parent.parent / "db" / "schema.sql"

MINT_PREFIX = "GRAD"


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = OFF")   # disable FK for easier seeding
    c.executescript(SCHEMA_SQL.read_text())
    yield c
    c.close()


def _seed_token(conn, mint: str, symbol: str = "SYM") -> None:
    conn.execute(
        """INSERT OR IGNORE INTO tokens
           (mint, symbol, name, launchpad, created_at, narrative_tags)
           VALUES (?, ?, ?, 'pump.fun', ?, '[]')""",
        (mint, symbol, f"Token {symbol}", int(time.time())),
    )


def _seed_outcome(conn, mint: str, classified: str, offset_h: int = 4) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO coin_outcomes
           (token_mint, check_offset_h, checked_at, classified)
           VALUES (?, ?, ?, ?)""",
        (mint, offset_h, int(time.time()), classified),
    )


def _seed_team_cluster(
    conn, mint: str, supply_pct: float, is_sniper: int = 0
) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO team_clusters
           (cluster_id, token_mint, supply_pct_at_graduation, is_bc_sniper, member_addresses)
           VALUES (?, ?, ?, ?, '[]')""",
        (str(uuid.uuid4()), mint, supply_pct, is_sniper),
    )


def _seed_grad_event(conn, mint: str, lag: int = 5) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO graduation_events
           (token_mint, graduated_at, detection_lag_seconds)
           VALUES (?, ?, ?)""",
        (mint, int(time.time()), lag),
    )


# ── is_significant enforcement ────────────────────────────────────────────────

def test_below_min_n_is_not_significant(conn):
    """With fewer than MIN_SIGNIFICANT_N rows, is_significant must be False."""
    for i in range(5):   # only 5 rows — well below threshold
        mint = f"MINT{'A' * 40}{i:02d}"[:44]
        _seed_token(conn, mint)
        _seed_outcome(conn, mint, "rug")
        _seed_team_cluster(conn, mint, 30.0, is_sniper=1)
    conn.commit()

    results = rug_rate_by_sniper_flag(conn)
    sniper_result = next((r for r in results if "is_bc_sniper" in r.label), None)
    assert sniper_result is not None
    assert sniper_result.is_significant is False
    assert "insufficient data" in sniper_result.note


def test_above_min_n_is_significant(conn):
    """With MIN_SIGNIFICANT_N or more rows, is_significant must be True."""
    for i in range(MIN_SIGNIFICANT_N):
        mint = f"SIG{'A' * 39}{i:02d}"[:44]
        _seed_token(conn, mint)
        _seed_outcome(conn, mint, "rug" if i % 3 == 0 else "ok")
        _seed_team_cluster(conn, mint, 25.0, is_sniper=1)
    conn.commit()

    results = rug_rate_by_sniper_flag(conn)
    sniper_result = next((r for r in results if "is_bc_sniper" in r.label), None)
    assert sniper_result is not None
    assert sniper_result.is_significant is True


def test_sample_size_matches_actual_row_count(conn):
    """sample_size in PatternResult must equal the actual matching row count."""
    n_rows = 7
    for i in range(n_rows):
        mint = f"COUNT{'A' * 37}{i:02d}"[:44]
        _seed_token(conn, mint)
        _seed_outcome(conn, mint, "ok")
        _seed_team_cluster(conn, mint, 10.0, is_sniper=0)
    conn.commit()

    results = rug_rate_by_sniper_flag(conn)
    non_sniper = next((r for r in results if "not_bc_sniper" in r.label), None)
    assert non_sniper is not None
    assert non_sniper.sample_size == n_rows


# ── PatternResult data contract ───────────────────────────────────────────────

def test_pattern_result_value_is_rate(conn):
    """Value should be a rug rate between 0 and 1."""
    conn.commit()
    results = rug_rate_by_team_supply_pct(conn)
    for r in results:
        assert 0.0 <= r.value <= 1.0


def test_detection_lag_returns_single_result(conn):
    for i in range(3):
        mint = f"LAG{'A' * 39}{i:02d}"[:44]
        _seed_token(conn, mint)
        _seed_grad_event(conn, mint, lag=10 + i * 5)
    conn.commit()

    result = avg_detection_lag_seconds(conn)
    assert isinstance(result, PatternResult)
    assert result.sample_size == 3
    assert result.value > 0


def test_empty_db_returns_zero_values(conn):
    """No data → all rates should be 0.0, all is_significant False."""
    results = rug_rate_by_sniper_flag(conn)
    for r in results:
        assert r.value == 0.0
        assert r.is_significant is False


# ── Rug rate accuracy ─────────────────────────────────────────────────────────

def test_rug_rate_computed_correctly(conn):
    """Seed 30 rug + 10 ok for snipers → rug_rate should be 0.75."""
    for i in range(30):
        mint = f"RUG{'A' * 39}{i:02d}"[:44]
        _seed_token(conn, mint)
        _seed_outcome(conn, mint, "rug")
        _seed_team_cluster(conn, mint, 40.0, is_sniper=1)
    for i in range(10):
        mint = f"OK{'A' * 40}{i:02d}"[:44]
        _seed_token(conn, mint)
        _seed_outcome(conn, mint, "ok")
        _seed_team_cluster(conn, mint, 40.0, is_sniper=1)
    conn.commit()

    results = rug_rate_by_sniper_flag(conn)
    sniper = next(r for r in results if "is_bc_sniper" in r.label)
    assert sniper.is_significant is True
    assert abs(sniper.value - 0.75) < 0.01


# ── distribution_signal_vs_outcome ────────────────────────────────────────────

def test_distribution_signal_returns_one_row_per_signal(conn):
    results = distribution_signal_vs_outcome(conn)
    labels = [r.label for r in results]
    assert any("ACCUMULATING" in l for l in labels)
    assert any("HOLDING" in l for l in labels)
    assert any("DISTRIBUTING" in l for l in labels)
    assert any("DUMPED" in l for l in labels)
