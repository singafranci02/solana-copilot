"""Tests for src/analyzer/narrative_match.py — in-memory SQLite, no network calls."""

import json
import sqlite3
import time
from pathlib import Path

import pytest

from src.analyzer.narrative_match import (
    get_active_narratives,
    match_token_to_narratives,
    narrative_velocity_at_entry,
)
from src.common.models import NarrativeState, Token

SCHEMA_SQL = Path(__file__).parent.parent / "db" / "schema.sql"


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.executescript(SCHEMA_SQL.read_text())
    yield c
    c.close()


def _seed_narrative(conn, label: str, keywords: list[str], status: str = "hot",
                    velocity: float = 60.0, peak: float = 80.0) -> int:
    cur = conn.execute(
        """INSERT INTO narratives (label, keywords, started_at, peak_velocity, current_velocity, status)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (label, json.dumps(keywords), int(time.time()) - 7200, peak, velocity, status),
    )
    conn.commit()
    return cur.lastrowid


def _token(symbol: str = "TEST", name: str = "Test Token",
           narrative_tags: list[str] | None = None) -> Token:
    return Token(
        mint="TOKENaaa",
        symbol=symbol,
        name=name,
        launchpad="pump.fun",
        created_at=int(time.time()),
        narrative_tags=narrative_tags or [],
    )


# ── get_active_narratives ─────────────────────────────────────────────────────

def test_get_active_narratives_returns_hot_and_emerging(conn):
    _seed_narrative(conn, "ai_agents", ["AI", "agent"], status="hot")
    _seed_narrative(conn, "dogecat", ["doge", "cat"], status="emerging", velocity=10.0)
    _seed_narrative(conn, "dead_meme", ["rip"], status="dead", velocity=0.0)

    results = get_active_narratives(conn)
    labels = [n.label for n in results]
    assert "ai_agents" in labels
    assert "dogecat" in labels
    assert "dead_meme" not in labels


def test_get_active_narratives_excludes_fading(conn):
    _seed_narrative(conn, "fading_thing", ["x"], status="fading")
    results = get_active_narratives(conn)
    assert all(n.label != "fading_thing" for n in results)


def test_get_active_narratives_ordered_by_velocity(conn):
    _seed_narrative(conn, "slow", ["slow"], velocity=10.0)
    _seed_narrative(conn, "fast", ["fast"], velocity=90.0)
    results = get_active_narratives(conn)
    velocities = [n.current_velocity for n in results]
    assert velocities == sorted(velocities, reverse=True)


def test_get_active_narratives_empty_when_none(conn):
    assert get_active_narratives(conn) == []


def test_get_active_narratives_keywords_parsed(conn):
    _seed_narrative(conn, "ai", ["artificial", "intelligence"])
    results = get_active_narratives(conn)
    assert results[0].keywords == ["artificial", "intelligence"]


# ── match_token_to_narratives ─────────────────────────────────────────────────

def test_match_exact_symbol():
    narratives = [NarrativeState(id=1, label="ai", keywords=["AI"], started_at=0)]
    t = _token(symbol="AI")
    assert "ai" in match_token_to_narratives(t, narratives)


def test_match_keyword_in_name():
    narratives = [NarrativeState(id=1, label="pepe", keywords=["pepe"], started_at=0)]
    t = _token(name="Pepe the Frog")
    assert "pepe" in match_token_to_narratives(t, narratives)


def test_match_narrative_tag():
    narratives = [NarrativeState(id=1, label="doge", keywords=["dogecoin"], started_at=0)]
    t = _token(narrative_tags=["dogecoin"])
    assert "doge" in match_token_to_narratives(t, narratives)


def test_match_fuzzy_close_spelling():
    narratives = [NarrativeState(id=1, label="base", keywords=["based"], started_at=0)]
    t = _token(symbol="BSED")
    assert "base" in match_token_to_narratives(t, narratives)


def test_no_match_returns_empty():
    narratives = [NarrativeState(id=1, label="pepe", keywords=["pepe"], started_at=0)]
    t = _token(symbol="MOON", name="MoonCoin")
    assert match_token_to_narratives(t, narratives) == []


def test_match_multiple_narratives():
    narratives = [
        NarrativeState(id=1, label="ai", keywords=["AI"], started_at=0),
        NarrativeState(id=2, label="pepe", keywords=["pepe"], started_at=0),
    ]
    t = _token(symbol="AIPEPE", name="AI Pepe")
    matched = match_token_to_narratives(t, narratives)
    assert "ai" in matched
    assert "pepe" in matched


def test_match_empty_narratives():
    t = _token(symbol="TEST")
    assert match_token_to_narratives(t, []) == []


def test_fuzzy_short_keyword_not_matched():
    # keywords < 4 chars are not fuzzy-matched — avoid false positives
    narratives = [NarrativeState(id=1, label="cat", keywords=["cat"], started_at=0)]
    t = _token(symbol="CAB")  # edit distance 1 from "cat" but too short for fuzzy
    # Exact match would not fire because "cab" != "cat" and "cat" not in "cab"
    assert match_token_to_narratives(t, narratives) == []


# ── narrative_velocity_at_entry ───────────────────────────────────────────────

def test_velocity_counts_mentions_in_window(conn):
    nar_id = _seed_narrative(conn, "ai", ["AI"])
    now = int(time.time())

    # 3 mentions in the last hour
    for offset in (100, 500, 1800):
        conn.execute(
            "INSERT INTO narrative_mentions (narrative_id, x_handle, posted_at) VALUES (?, ?, ?)",
            (nar_id, f"user{offset}", now - offset),
        )
    conn.commit()

    v = narrative_velocity_at_entry("ai", now, conn)
    assert v == pytest.approx(3.0)


def test_velocity_excludes_mentions_outside_window(conn):
    nar_id = _seed_narrative(conn, "ai", ["AI"])
    now = int(time.time())

    # 1 mention within the hour, 1 older than 1h
    conn.execute(
        "INSERT INTO narrative_mentions (narrative_id, x_handle, posted_at) VALUES (?, ?, ?)",
        (nar_id, "user1", now - 500),
    )
    conn.execute(
        "INSERT INTO narrative_mentions (narrative_id, x_handle, posted_at) VALUES (?, ?, ?)",
        (nar_id, "user2", now - 7200),
    )
    conn.commit()

    v = narrative_velocity_at_entry("ai", now, conn)
    assert v == pytest.approx(1.0)


def test_velocity_zero_when_no_mentions(conn):
    _seed_narrative(conn, "ai", ["AI"])
    assert narrative_velocity_at_entry("ai", int(time.time()), conn) == pytest.approx(0.0)


def test_velocity_unknown_label_returns_zero(conn):
    assert narrative_velocity_at_entry("nonexistent", int(time.time()), conn) == pytest.approx(0.0)
