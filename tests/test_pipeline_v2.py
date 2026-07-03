"""Pipeline v2 plumbing: creation extraction, trade-window fetch, api_usage."""

import pytest

from src.analyzer.project_classifier import extract_creation
from src.common.api_usage import normalize_endpoint
from src.ingest.solana_tracker import SolanaTrackerClient


def test_extract_creation_seconds_and_ms():
    raw = {"token": {"creation": {
        "creator": "AVzP2GeRmqGphJsMxWoqjpUifPpCret7LqWhD8NWQK49",
        "created_time": 1_750_000_000,
    }}}
    creator, created = extract_creation(raw)
    assert creator == "AVzP2GeRmqGphJsMxWoqjpUifPpCret7LqWhD8NWQK49"
    assert created == 1_750_000_000
    # milliseconds normalized to seconds
    raw["token"]["creation"]["created_time"] = 1_750_000_000_000
    assert extract_creation(raw)[1] == 1_750_000_000


def test_extract_creation_rejects_placeholder_creator():
    raw = {"token": {"creation": {
        "creator": "11111111111111111111111111111111",   # system program (WSOL-style)
        "created_time": 1_601_221_070,
    }}}
    creator, created = extract_creation(raw)
    assert creator is None
    assert created == 1_601_221_070
    assert extract_creation(None) == (None, None)
    assert extract_creation({"token": {}}) == (None, None)


def test_normalize_endpoint_collapses_addresses():
    p = "/tokens/6yvEyLV6JyRQq1Jsm2Ys7iEb6pDtkqi6RygycyiXpump/holders"
    assert normalize_endpoint(p) == "/tokens/{mint}/holders"
    assert normalize_endpoint("/price") == "/price"


class _FakeSession:
    """Serves canned cursor pages for get_token_trades."""

    def __init__(self, pages):
        self._pages = pages
        self.calls = 0

    def get(self, url, params=None):
        page = self._pages[min(self.calls, len(self._pages) - 1)]
        self.calls += 1
        return _FakeResp(page)


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload
        self.status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def raise_for_status(self):
        pass

    async def json(self):
        return self._payload


def _trade(wallet, ts_s, type_="buy"):
    return {"type": type_, "wallet": wallet, "volumeSol": 1.0,
            "amount": 100.0, "time": ts_s * 1000}


@pytest.mark.asyncio
async def test_get_token_trades_asc_until_ts_early_stop():
    w = "AVzP2GeRmqGphJsMxWoqjpUifPpCret7LqWhD8NWQK49"
    t0 = 1_750_000_000   # realistic epoch so the ms→s guard in _trade_to_swap fires
    pages = [
        {"trades": [_trade(w, t0 + 100), _trade(w, t0 + 200)], "hasNextPage": True, "nextCursor": "c1"},
        {"trades": [_trade(w, t0 + 300), _trade(w, t0 + 999)], "hasNextPage": True, "nextCursor": "c2"},
        {"trades": [_trade(w, t0 + 2000)], "hasNextPage": False},
    ]
    client = SolanaTrackerClient(api_key="test")
    client._session = _FakeSession(pages)
    swaps = await client.get_token_trades(
        "M" * 44, since_ts=t0 + 100, until_ts=t0 + 500, sort="ASC",
    )
    # trades past until_ts excluded; early-stop after page 2 (t0+999 > t0+500)
    assert [s.timestamp for s in swaps] == [t0 + 100, t0 + 200, t0 + 300]
    assert client._session.calls == 2


@pytest.mark.asyncio
async def test_get_token_trades_respects_configured_page_cap(monkeypatch):
    from src.common.config import settings
    monkeypatch.setattr(settings, "trades_max_pages", 2)
    w = "AVzP2GeRmqGphJsMxWoqjpUifPpCret7LqWhD8NWQK49"
    endless = {"trades": [_trade(w, 100)], "hasNextPage": True, "nextCursor": "c"}
    client = SolanaTrackerClient(api_key="test")
    client._session = _FakeSession([endless])
    await client.get_token_trades("M" * 44, sort="ASC")
    assert client._session.calls == 2
