"""Tests for src/ingest/gmgn.py — no real network calls."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from src.ingest.gmgn import GMGNClient, parse_token_info, parse_wallet_profile

# ── shared mock helpers ───────────────────────────────────────────────────────

WALLET_ADDR = "SMARTaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
TOKEN_MINT  = "TOKENaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def _mock_resp(data, status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.headers = {}
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=data)
    return resp


@pytest.fixture
def client():
    c = GMGNClient(api_key="test-key")
    c._http = MagicMock()
    return c


# ── raw response fixtures ─────────────────────────────────────────────────────

WALLET_PROFILE_RESP = {
    "code": 0,
    "msg": "ok",
    "data": {
        "realized_profit": 15_234.5,
        "buy": 145,
        "sell": 118,
        "winrate": 0.73,
        "last_active_timestamp": 1_705_312_800,
        "tags": ["smart_degen"],
    },
}

TOKEN_INFO_RESP = {
    "code": 0,
    "msg": "ok",
    "data": {
        "symbol": "PEPETOPIA",
        "name": "Pepetopia",
        "market_cap": 50_000.0,
        "holder_count": 342,
        "burn_status": "burned",
        "top10_holder_rate": 0.35,
        "launchpad": "pump.fun",
        "open_timestamp": 1_705_312_800,
    },
}

TOP_TRADERS_RESP = {
    "code": 0,
    "data": [
        {"address": WALLET_ADDR, "profit": 1_234.5, "buy_amount": 2.5, "sell_amount": 3.7},
        {"address": "TRADER2aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "profit": 890.0},
    ],
}


# ── parse_wallet_profile ──────────────────────────────────────────────────────

def test_parse_wallet_profile_win_rate():
    w = parse_wallet_profile(WALLET_ADDR, WALLET_PROFILE_RESP)
    assert w.win_rate_90d == pytest.approx(0.73)


def test_parse_wallet_profile_total_trades():
    w = parse_wallet_profile(WALLET_ADDR, WALLET_PROFILE_RESP)
    assert w.total_trades == 145 + 118


def test_parse_wallet_profile_label_from_tags():
    w = parse_wallet_profile(WALLET_ADDR, WALLET_PROFILE_RESP)
    assert w.label == "smart_degen"


def test_parse_wallet_profile_first_seen():
    w = parse_wallet_profile(WALLET_ADDR, WALLET_PROFILE_RESP)
    assert w.first_seen == 1_705_312_800


def test_parse_wallet_profile_address_preserved():
    w = parse_wallet_profile(WALLET_ADDR, WALLET_PROFILE_RESP)
    assert w.address == WALLET_ADDR


def test_parse_wallet_profile_score_starts_at_zero():
    # score_wallet() is responsible for populating smart_money_score
    w = parse_wallet_profile(WALLET_ADDR, WALLET_PROFILE_RESP)
    assert w.smart_money_score == 0.0


def test_parse_wallet_profile_no_tags():
    raw = {"code": 0, "data": {"winrate": 0.5, "buy": 10, "sell": 8, "tags": []}}
    w = parse_wallet_profile(WALLET_ADDR, raw)
    assert w.label is None


def test_parse_wallet_profile_missing_fields():
    # Minimal response: everything optional absent
    w = parse_wallet_profile(WALLET_ADDR, {"code": 0, "data": {}})
    assert w.win_rate_90d is None
    assert w.total_trades == 0
    assert w.first_seen is None


# ── parse_token_info ──────────────────────────────────────────────────────────

def test_parse_token_info_symbol():
    t = parse_token_info(TOKEN_MINT, TOKEN_INFO_RESP)
    assert t.symbol == "PEPETOPIA"


def test_parse_token_info_launchpad_pump():
    t = parse_token_info(TOKEN_MINT, TOKEN_INFO_RESP)
    assert t.launchpad == "pump.fun"


def test_parse_token_info_launchpad_bags():
    raw = {**TOKEN_INFO_RESP, "data": {**TOKEN_INFO_RESP["data"], "launchpad": "bags.fm"}}
    t = parse_token_info(TOKEN_MINT, raw)
    assert t.launchpad == "bags"


def test_parse_token_info_launchpad_unknown():
    raw = {**TOKEN_INFO_RESP, "data": {**TOKEN_INFO_RESP["data"], "launchpad": "raydium"}}
    t = parse_token_info(TOKEN_MINT, raw)
    assert t.launchpad == "unknown"


def test_parse_token_info_lp_burned_true():
    t = parse_token_info(TOKEN_MINT, TOKEN_INFO_RESP)
    assert t.lp_burned is True


def test_parse_token_info_lp_burned_false():
    raw = {"code": 0, "data": {**TOKEN_INFO_RESP["data"], "burn_status": "not_burned"}}
    t = parse_token_info(TOKEN_MINT, raw)
    assert t.lp_burned is False


def test_parse_token_info_top10_pct_fraction_normalised():
    # 0.35 fraction → 35.0 percent
    t = parse_token_info(TOKEN_MINT, TOKEN_INFO_RESP)
    assert t.top10_pct == pytest.approx(35.0)


def test_parse_token_info_top10_pct_already_percent():
    raw = {"code": 0, "data": {**TOKEN_INFO_RESP["data"], "top10_holder_rate": 42.0}}
    t = parse_token_info(TOKEN_MINT, raw)
    assert t.top10_pct == pytest.approx(42.0)


def test_parse_token_info_market_cap():
    t = parse_token_info(TOKEN_MINT, TOKEN_INFO_RESP)
    assert t.market_cap_usd_snapshot == pytest.approx(50_000.0)


def test_parse_token_info_holders():
    t = parse_token_info(TOKEN_MINT, TOKEN_INFO_RESP)
    assert t.holders_count_snapshot == 342


def test_parse_token_info_mint_preserved():
    t = parse_token_info(TOKEN_MINT, TOKEN_INFO_RESP)
    assert t.mint == TOKEN_MINT


# ── GMGNClient HTTP methods ───────────────────────────────────────────────────

async def test_get_wallet_profile_calls_correct_path(client):
    client._http.get = AsyncMock(return_value=_mock_resp(WALLET_PROFILE_RESP))
    result = await client.get_wallet_profile(WALLET_ADDR)
    assert result["code"] == 0
    call_url = client._http.get.call_args[0][0]
    assert WALLET_ADDR in call_url
    assert "walletNew" in call_url


async def test_get_token_info_calls_correct_path(client):
    client._http.get = AsyncMock(return_value=_mock_resp(TOKEN_INFO_RESP))
    await client.get_token_info(TOKEN_MINT)
    call_url = client._http.get.call_args[0][0]
    assert TOKEN_MINT in call_url


async def test_get_token_top_traders_returns_list(client):
    client._http.get = AsyncMock(return_value=_mock_resp(TOP_TRADERS_RESP))
    traders = await client.get_token_top_traders(TOKEN_MINT, limit=2)
    assert isinstance(traders, list)
    assert len(traders) == 2
    assert traders[0]["address"] == WALLET_ADDR


async def test_get_token_top_traders_bare_list(client):
    # Some GMGN endpoints return a bare list instead of {"data": [...]}
    client._http.get = AsyncMock(return_value=_mock_resp([{"address": WALLET_ADDR}]))
    traders = await client.get_token_top_traders(TOKEN_MINT)
    assert traders[0]["address"] == WALLET_ADDR


async def test_retry_on_429(client):
    rate_limited = _mock_resp(None, status=429)
    rate_limited.headers = {"Retry-After": "0"}
    ok = _mock_resp(WALLET_PROFILE_RESP)

    client._http.get = AsyncMock(side_effect=[rate_limited, ok])
    result = await client.get_wallet_profile(WALLET_ADDR)
    assert result["code"] == 0
    assert client._http.get.call_count == 2


async def test_gmgn_application_error_raises(client):
    error_resp = {"code": 400, "msg": "wallet not found"}
    client._http.get = AsyncMock(return_value=_mock_resp(error_resp))
    with pytest.raises(Exception, match="GMGN error"):
        await client.get_wallet_profile(WALLET_ADDR)


async def test_client_context_manager(tmp_path):
    async with GMGNClient(api_key="key") as c:
        assert c._http is not None
