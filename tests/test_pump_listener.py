"""Tests for src/ingest/pump_listener.py and src/services/pump_monitor.py parsers."""

import pytest

from src.ingest.pump_listener import _parse_coin, _parse_trade
from src.services.pump_monitor import _twitter_handle, extract_narrative_tags
from src.ingest.pump_listener import NewCoin


# ── shared fixtures ───────────────────────────────────────────────────────────

RAW_COIN = {
    "mint": "MINTaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "name": "Pepe the AI Dog",
    "symbol": "PEPEAI",
    "description": "The most based AI pepe dog on Solana. MAGA.",
    "creator": "CREATORaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "created_timestamp": 1_705_312_800,
    "twitter": "https://x.com/pepeai",
    "telegram": "https://t.me/pepeai",
    "website": "https://pepeai.fun",
    "image_uri": "https://cf-ipfs.com/ipfs/Qm123",
    "usd_market_cap": 12_500.0,
}

RAW_TRADE_BUY = {
    "mint": "MINTaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "user": "BUYERaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "sol_amount": 500_000_000,   # 0.5 SOL in lamports
    "token_amount": 1_000_000.0,
    "is_buy": True,
    "timestamp": 1_705_312_860,
}

RAW_TRADE_SELL = {**RAW_TRADE_BUY, "is_buy": False, "user": "SELLERaaaaaaaaaaaaaaa"}


# ── _parse_coin ───────────────────────────────────────────────────────────────

def test_parse_coin_mint():
    c = _parse_coin(RAW_COIN)
    assert c.mint == RAW_COIN["mint"]


def test_parse_coin_name_symbol():
    c = _parse_coin(RAW_COIN)
    assert c.name == "Pepe the AI Dog"
    assert c.symbol == "PEPEAI"


def test_parse_coin_description():
    c = _parse_coin(RAW_COIN)
    assert "AI pepe" in c.description


def test_parse_coin_creator():
    c = _parse_coin(RAW_COIN)
    assert c.creator == RAW_COIN["creator"]


def test_parse_coin_timestamp():
    c = _parse_coin(RAW_COIN)
    assert c.created_timestamp == 1_705_312_800


def test_parse_coin_social_links():
    c = _parse_coin(RAW_COIN)
    assert c.twitter == "https://x.com/pepeai"
    assert c.telegram == "https://t.me/pepeai"


def test_parse_coin_market_cap():
    c = _parse_coin(RAW_COIN)
    assert c.market_cap_usd == pytest.approx(12_500.0)


def test_parse_coin_missing_mint_returns_none():
    assert _parse_coin({}) is None
    assert _parse_coin({"name": "test"}) is None


def test_parse_coin_optional_fields_nullable():
    minimal = {"mint": "MINTaaaa", "name": "X", "symbol": "X", "creator": "C",
               "created_timestamp": 0}
    c = _parse_coin(minimal)
    assert c.twitter is None
    assert c.market_cap_usd is None


# ── _parse_trade ──────────────────────────────────────────────────────────────

def test_parse_trade_sol_converted_from_lamports():
    t = _parse_trade(RAW_TRADE_BUY)
    assert t.sol_amount == pytest.approx(0.5)


def test_parse_trade_is_buy_true():
    t = _parse_trade(RAW_TRADE_BUY)
    assert t.is_buy is True


def test_parse_trade_is_buy_false():
    t = _parse_trade(RAW_TRADE_SELL)
    assert t.is_buy is False


def test_parse_trade_user():
    t = _parse_trade(RAW_TRADE_BUY)
    assert t.user == RAW_TRADE_BUY["user"]


def test_parse_trade_mint():
    t = _parse_trade(RAW_TRADE_BUY)
    assert t.mint == RAW_TRADE_BUY["mint"]


def test_parse_trade_token_amount():
    t = _parse_trade(RAW_TRADE_BUY)
    assert t.token_amount == pytest.approx(1_000_000.0)


def test_parse_trade_missing_mint_returns_none():
    assert _parse_trade({"user": "abc"}) is None


def test_parse_trade_missing_user_returns_none():
    assert _parse_trade({"mint": "abc"}) is None


def test_parse_trade_zero_sol_amount():
    raw = {**RAW_TRADE_BUY, "sol_amount": 0}
    t = _parse_trade(raw)
    assert t.sol_amount == pytest.approx(0.0)


# ── extract_narrative_tags ────────────────────────────────────────────────────

def _coin(name="", symbol="", description="") -> NewCoin:
    return NewCoin(
        mint="x", name=name, symbol=symbol, description=description,
        creator="", created_timestamp=0,
    )


def test_extract_pepe_from_name():
    tags = extract_narrative_tags(_coin(name="Pepe Token"))
    assert "pepe" in tags


def test_extract_ai_from_description():
    tags = extract_narrative_tags(_coin(description="AI agent on Solana"))
    assert "ai" in tags


def test_extract_multiple_narratives():
    tags = extract_narrative_tags(_coin(name="Trump Doge"))
    assert "trump" in tags
    assert "doge" in tags


def test_extract_deduplicates_tags():
    # "doge" and "dog" both map to "doge" — should appear once
    tags = extract_narrative_tags(_coin(name="Doge Dog"))
    assert tags.count("doge") == 1


def test_extract_no_match_returns_empty():
    tags = extract_narrative_tags(_coin(name="MOON", symbol="MOON", description="to the moon"))
    assert tags == []


def test_extract_case_insensitive():
    tags = extract_narrative_tags(_coin(symbol="PEPE"))
    assert "pepe" in tags


# ── _twitter_handle ───────────────────────────────────────────────────────────

def test_twitter_handle_x_com():
    assert _twitter_handle("https://x.com/pepeai") == "pepeai"


def test_twitter_handle_twitter_com():
    assert _twitter_handle("https://twitter.com/pepeai") == "pepeai"


def test_twitter_handle_with_trailing_slash():
    assert _twitter_handle("https://x.com/pepeai/") == "pepeai"


def test_twitter_handle_with_query_string():
    assert _twitter_handle("https://x.com/pepeai?ref=abc") == "pepeai"


def test_twitter_handle_none():
    assert _twitter_handle(None) is None


def test_twitter_handle_empty():
    assert _twitter_handle("") is None


def test_twitter_handle_unknown_url():
    assert _twitter_handle("https://instagram.com/pepeai") is None
