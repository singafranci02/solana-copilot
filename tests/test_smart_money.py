"""Tests for src/analyzer/smart_money.py — in-memory SQLite, no network calls."""

import sqlite3
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.analyzer.smart_money import (
    SMART_MONEY_THRESHOLD,
    enrich_wallet,
    find_smart_money_in_buyers,
    get_smart_money_wallets,
    score_wallet,
    upsert_wallet,
)
from src.common.models import TokenBuyer, Wallet

SCHEMA_SQL = Path(__file__).parent.parent / "db" / "schema.sql"

WALLET_A = "SMARTaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
WALLET_B = "SMARTbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
WALLET_C = "SMARTcccccccccccccccccccccccccccccccccccccc"
TOKEN_MINT = "TOKENaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


# ── fixture ───────────────────────────────────────────────────────────────────

@pytest.fixture
def conn():
    """In-memory SQLite with full schema applied."""
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.executescript(SCHEMA_SQL.read_text())
    yield c
    c.close()


def _wallet(address: str, score: float = 0.0, win_rate: float | None = None,
            total_trades: int = 0) -> Wallet:
    return Wallet(
        address=address,
        smart_money_score=score,
        win_rate_90d=win_rate,
        total_trades=total_trades,
    )


def _seed_wallet(conn: sqlite3.Connection, wallet: Wallet) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO wallets
           (address, label, smart_money_score, win_rate_90d, total_trades, first_seen, funding_source)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (wallet.address, wallet.label, wallet.smart_money_score,
         wallet.win_rate_90d, wallet.total_trades, wallet.first_seen,
         wallet.funding_source),
    )
    conn.commit()


def _seed_token(conn: sqlite3.Connection) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO tokens
           (mint, symbol, name, launchpad, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (TOKEN_MINT, "TST", "Test Token", "pump.fun", int(time.time()) - 86_400),
    )
    conn.commit()


# ── upsert_wallet ─────────────────────────────────────────────────────────────

def test_upsert_wallet_inserts_new_row(conn):
    w = _wallet(WALLET_A, score=0.5, win_rate=0.6, total_trades=100)
    upsert_wallet(w, conn)
    row = conn.execute("SELECT * FROM wallets WHERE address = ?", (WALLET_A,)).fetchone()
    assert row is not None
    assert float(row["smart_money_score"]) == pytest.approx(0.5)


def test_upsert_wallet_updates_existing_score(conn):
    w = _wallet(WALLET_A, score=0.3)
    upsert_wallet(w, conn)
    w2 = _wallet(WALLET_A, score=0.9)
    upsert_wallet(w2, conn)
    row = conn.execute("SELECT smart_money_score FROM wallets WHERE address = ?", (WALLET_A,)).fetchone()
    assert float(row["smart_money_score"]) == pytest.approx(0.9)


def test_upsert_wallet_preserves_existing_funding_source(conn):
    w = _wallet(WALLET_A)
    w.funding_source = "original_funder"
    upsert_wallet(w, conn)

    w2 = _wallet(WALLET_A)
    w2.funding_source = "new_funder"
    upsert_wallet(w2, conn)

    row = conn.execute("SELECT funding_source FROM wallets WHERE address = ?", (WALLET_A,)).fetchone()
    assert row["funding_source"] == "original_funder"


def test_upsert_wallet_sets_funding_source_when_previously_null(conn):
    w = _wallet(WALLET_A)
    w.funding_source = None
    upsert_wallet(w, conn)

    w2 = _wallet(WALLET_A)
    w2.funding_source = "new_funder"
    upsert_wallet(w2, conn)

    row = conn.execute("SELECT funding_source FROM wallets WHERE address = ?", (WALLET_A,)).fetchone()
    assert row["funding_source"] == "new_funder"


# ── get_smart_money_wallets ───────────────────────────────────────────────────

def test_get_smart_money_wallets_filters_by_threshold(conn):
    _seed_wallet(conn, _wallet(WALLET_A, score=0.85))
    _seed_wallet(conn, _wallet(WALLET_B, score=0.50))
    _seed_wallet(conn, _wallet(WALLET_C, score=0.70))  # exactly at threshold

    results = get_smart_money_wallets(conn)
    addresses = {w.address for w in results}
    assert WALLET_A in addresses
    assert WALLET_C in addresses
    assert WALLET_B not in addresses


def test_get_smart_money_wallets_ordered_best_first(conn):
    _seed_wallet(conn, _wallet(WALLET_A, score=0.72))
    _seed_wallet(conn, _wallet(WALLET_B, score=0.95))
    _seed_wallet(conn, _wallet(WALLET_C, score=0.80))

    results = get_smart_money_wallets(conn)
    scores = [w.smart_money_score for w in results]
    assert scores == sorted(scores, reverse=True)


def test_get_smart_money_wallets_empty_when_none_qualify(conn):
    _seed_wallet(conn, _wallet(WALLET_A, score=0.3))
    assert get_smart_money_wallets(conn) == []


def test_get_smart_money_wallets_threshold_constant():
    assert SMART_MONEY_THRESHOLD == pytest.approx(0.7)


# ── score_wallet ──────────────────────────────────────────────────────────────

def test_score_wallet_high_win_rate_dominates(conn):
    w = _wallet(WALLET_A, win_rate=1.0, total_trades=500)
    _seed_wallet(conn, w)
    score = score_wallet(w, conn)
    # 0.60*1.0 + 0.25*1.0 + 0.15*0.0(no recency) = 0.85
    assert score == pytest.approx(0.85, abs=0.01)


def test_score_wallet_zero_win_rate(conn):
    w = _wallet(WALLET_A, win_rate=0.0, total_trades=0)
    _seed_wallet(conn, w)
    score = score_wallet(w, conn)
    assert score == pytest.approx(0.0, abs=0.01)


def test_score_wallet_writes_back_to_db(conn):
    w = _wallet(WALLET_A, win_rate=0.8, total_trades=200)
    _seed_wallet(conn, w)
    score = score_wallet(w, conn)

    row = conn.execute("SELECT smart_money_score FROM wallets WHERE address = ?", (WALLET_A,)).fetchone()
    assert float(row["smart_money_score"]) == pytest.approx(score)


def test_score_wallet_volume_signal_saturates_at_500(conn):
    w_high = _wallet(WALLET_A, win_rate=0.0, total_trades=1000)
    w_mid = _wallet(WALLET_B, win_rate=0.0, total_trades=500)
    _seed_wallet(conn, w_high)
    _seed_wallet(conn, w_mid)

    s_high = score_wallet(w_high, conn)
    s_mid = score_wallet(w_mid, conn)
    # Both hit the 1.0 cap for volume; recency is the same (no token_buyers)
    assert s_high == pytest.approx(s_mid, abs=0.001)


def test_score_wallet_recency_from_token_buyers(conn):
    """A recent buy in token_buyers boosts the recency factor."""
    _seed_token(conn)
    w = _wallet(WALLET_A, win_rate=0.5, total_trades=100)
    _seed_wallet(conn, w)

    now = int(time.time())
    conn.execute(
        """INSERT INTO token_buyers
           (token_mint, wallet_address, bought_at, sol_amount, tokens_received)
           VALUES (?, ?, ?, ?, ?)""",
        (TOKEN_MINT, WALLET_A, now, 1.0, 1_000_000.0),
    )
    conn.commit()

    score = score_wallet(w, conn)
    # recency ~ 1.0 (just bought); 0.60*0.5 + 0.25*(100/500) + 0.15*1.0
    expected = 0.60 * 0.5 + 0.25 * 0.2 + 0.15 * 1.0
    assert score == pytest.approx(expected, abs=0.01)


def test_score_wallet_recency_zero_when_idle_90_days(conn):
    w = _wallet(WALLET_A, win_rate=0.5, total_trades=0)
    _seed_wallet(conn, w)
    # No token_buyer rows → defaults to 90 days idle → recency = 0
    score = score_wallet(w, conn)
    expected = 0.60 * 0.5 + 0.25 * 0.0 + 0.15 * 0.0
    assert score == pytest.approx(expected, abs=0.001)


def test_score_wallet_clamped_to_0_1(conn):
    # Pathological: even max input should not exceed 1.0
    w = _wallet(WALLET_A, win_rate=2.0, total_trades=99999)
    _seed_wallet(conn, w)
    score = score_wallet(w, conn)
    assert 0.0 <= score <= 1.0


# ── find_smart_money_in_buyers ────────────────────────────────────────────────

def _buyer(wallet_address: str) -> TokenBuyer:
    return TokenBuyer(
        token_mint=TOKEN_MINT,
        wallet_address=wallet_address,
        bought_at=int(time.time()),
        sol_amount=1.0,
        tokens_received=1_000_000.0,
    )


def test_find_smart_money_in_buyers_overlap(conn):
    smart = [_wallet(WALLET_A, score=0.9), _wallet(WALLET_B, score=0.8)]
    buyers = [_buyer(WALLET_A), _buyer(WALLET_C)]
    result = find_smart_money_in_buyers(buyers, smart)
    assert len(result) == 1
    assert result[0].address == WALLET_A


def test_find_smart_money_in_buyers_no_overlap(conn):
    smart = [_wallet(WALLET_B, score=0.9)]
    buyers = [_buyer(WALLET_A)]
    assert find_smart_money_in_buyers(buyers, smart) == []


def test_find_smart_money_in_buyers_preserves_score_order(conn):
    smart = [_wallet(WALLET_A, score=0.95), _wallet(WALLET_B, score=0.75)]
    buyers = [_buyer(WALLET_B), _buyer(WALLET_A)]
    result = find_smart_money_in_buyers(buyers, smart)
    assert result[0].address == WALLET_A
    assert result[1].address == WALLET_B


def test_find_smart_money_in_buyers_empty_buyers(conn):
    smart = [_wallet(WALLET_A, score=0.9)]
    assert find_smart_money_in_buyers([], smart) == []


def test_find_smart_money_in_buyers_empty_smart(conn):
    buyers = [_buyer(WALLET_A)]
    assert find_smart_money_in_buyers(buyers, []) == []


# ── enrich_wallet ─────────────────────────────────────────────────────────────

def _mock_gmgn(profile_resp: dict) -> MagicMock:
    gmgn = MagicMock()
    gmgn.get_wallet_profile = AsyncMock(return_value=profile_resp)
    return gmgn


PROFILE_RESP = {
    "code": 0,
    "data": {
        "winrate": 0.80,
        "buy": 200,
        "sell": 150,
        "last_active_timestamp": 1_705_312_800,
        "tags": ["smart_degen"],
    },
}


async def test_enrich_wallet_returns_wallet_with_score(conn):
    gmgn = _mock_gmgn(PROFILE_RESP)
    wallet = await enrich_wallet(WALLET_A, gmgn, conn)
    assert wallet.address == WALLET_A
    assert wallet.smart_money_score > 0.0
    assert wallet.win_rate_90d == pytest.approx(0.80)


async def test_enrich_wallet_calls_gmgn_with_address(conn):
    gmgn = _mock_gmgn(PROFILE_RESP)
    await enrich_wallet(WALLET_A, gmgn, conn)
    gmgn.get_wallet_profile.assert_awaited_once_with(WALLET_A)


async def test_enrich_wallet_persists_to_db(conn):
    gmgn = _mock_gmgn(PROFILE_RESP)
    wallet = await enrich_wallet(WALLET_A, gmgn, conn)

    row = conn.execute(
        "SELECT smart_money_score, win_rate_90d, total_trades FROM wallets WHERE address = ?",
        (WALLET_A,),
    ).fetchone()
    assert row is not None
    assert float(row["smart_money_score"]) == pytest.approx(wallet.smart_money_score)
    assert float(row["win_rate_90d"]) == pytest.approx(0.80)
    assert int(row["total_trades"]) == 350


async def test_enrich_wallet_score_in_0_1(conn):
    gmgn = _mock_gmgn(PROFILE_RESP)
    wallet = await enrich_wallet(WALLET_A, gmgn, conn)
    assert 0.0 <= wallet.smart_money_score <= 1.0


async def test_enrich_wallet_idempotent(conn):
    """Calling enrich twice on the same address should not error or duplicate rows."""
    gmgn = _mock_gmgn(PROFILE_RESP)
    await enrich_wallet(WALLET_A, gmgn, conn)
    await enrich_wallet(WALLET_A, gmgn, conn)

    count = conn.execute("SELECT COUNT(*) FROM wallets WHERE address = ?", (WALLET_A,)).fetchone()[0]
    assert count == 1
