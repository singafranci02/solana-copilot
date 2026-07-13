"""Tests for src/ingest/graduation_monitor.py — parsing and BC-holder logic.

Network and DB calls are mocked so tests run fully offline.
"""

import time

import pytest

from src.ingest.graduation_monitor import _parse_bc_holders, _parse_migrate


# ── _parse_migrate ─────────────────────────────────────────────────────────────

def test_parse_migrate_standard_shape():
    raw = {
        "mint": "ABCDEFabcdefABCDEFabcdefABCDEFabcdefABCDEF44",
        "pool": "POOLabcdefPOOLabcdefPOOLabcdefPOOLabcdefPOOL44",
        "timestamp": 1_700_000_000,
    }
    event = _parse_migrate(raw)
    assert event is not None
    assert event.mint == raw["mint"]
    assert event.pool_address == raw["pool"]
    assert event.event_ts == 1_700_000_000


def test_parse_migrate_alternate_field_names():
    """Pump.fun may use different field names — test fallbacks."""
    raw = {
        "token": "TOKENaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "ammPool": "POOLbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    }
    event = _parse_migrate(raw)
    assert event is not None
    assert event.mint == raw["token"]
    assert event.pool_address == raw["ammPool"]


def test_parse_migrate_address_field_fallback():
    raw = {"address": "ADDRcccccccccccccccccccccccccccccccccccccccc"}
    event = _parse_migrate(raw)
    assert event is not None
    assert event.mint == raw["address"]
    assert event.pool_address is None


def test_parse_migrate_returns_none_when_no_mint():
    raw = {"pool": "some_pool", "timestamp": 1_700_000_000}
    event = _parse_migrate(raw)
    assert event is None


def test_parse_migrate_empty_dict():
    assert _parse_migrate({}) is None


def test_parse_migrate_timestamp_defaults_to_now():
    raw = {"mint": "MINTdddddddddddddddddddddddddddddddddddddddd"}
    before = int(time.time())
    event = _parse_migrate(raw)
    after = int(time.time())
    assert event is not None
    assert before <= event.event_ts <= after + 1


# ── _parse_bc_holders ─────────────────────────────────────────────────────────

def test_parse_bc_holders_computes_pct():
    accounts = [
        {"address": "wallet_a", "uiAmount": 600.0},
        {"address": "wallet_b", "uiAmount": 400.0},
    ]
    holders = _parse_bc_holders(accounts)
    assert len(holders) == 2
    # wallet_a holds 60%
    a = next(h for h in holders if h["wallet"] == "wallet_a")
    assert abs(a["pct"] - 60.0) < 0.01


def test_parse_bc_holders_excludes_zero_balance():
    accounts = [
        {"address": "wallet_a", "uiAmount": 1000.0},
        {"address": "wallet_b", "uiAmount": 0.0},
        {"address": "wallet_c", "uiAmount": None},
    ]
    holders = _parse_bc_holders(accounts)
    wallets = [h["wallet"] for h in holders]
    assert "wallet_a" in wallets
    assert "wallet_b" not in wallets
    assert "wallet_c" not in wallets


def test_parse_bc_holders_capped_at_top_20():
    accounts = [
        {"address": f"wallet_{i}", "uiAmount": float(100 - i)}
        for i in range(30)
    ]
    holders = _parse_bc_holders(accounts)
    assert len(holders) <= 20


def test_parse_bc_holders_empty_returns_empty():
    assert _parse_bc_holders([]) == []


def test_parse_bc_holders_total_zero_returns_empty():
    accounts = [{"address": "wallet_a", "uiAmount": 0.0}]
    assert _parse_bc_holders(accounts) == []


def test_parse_bc_holders_percentages_sum_to_100():
    accounts = [
        {"address": f"w{i}", "uiAmount": float(10 + i)}
        for i in range(5)
    ]
    holders = _parse_bc_holders(accounts)
    total_pct = sum(h["pct"] for h in holders)
    assert abs(total_pct - 100.0) < 0.1


def test_parse_bc_holders_preserves_ui_amount():
    accounts = [{"address": "wallet_a", "uiAmount": 250.0}]
    holders = _parse_bc_holders(accounts)
    assert holders[0]["ui_amount"] == 250.0


def test_venue_gate_pump_only():
    """PumpPortal streams other launchpads' migrations too — only pump.fun passes.
    None and real pool ADDRESSES (REST fallback) pass; foreign venue labels don't."""
    from src.ingest.graduation_monitor import _is_allowed_venue
    assert _is_allowed_venue("pump-amm")
    assert _is_allowed_venue("PUMP")
    assert _is_allowed_venue(None)
    assert _is_allowed_venue("6zFdgaXbAufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")
    assert not _is_allowed_venue("raydium-cpmm")
    assert not _is_allowed_venue("launchlab")
    assert not _is_allowed_venue("mayhem")
