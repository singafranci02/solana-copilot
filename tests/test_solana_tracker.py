"""Tests for src/ingest/solana_tracker.py — normalization + pagination (offline)."""

import pytest

from src.ingest.solana_tracker import SolanaTrackerClient, _trade_to_swap


def _raw(wallet, side="buy", amount=100.0, vol_sol=1.0, time=1000, tx="T1"):
    return {"wallet": wallet, "type": side, "amount": amount, "volumeSol": vol_sol,
            "priceUsd": 0.01, "time": time, "tx": tx, "program": "pumpswap"}


# ── _trade_to_swap normalization ────────────────────────────────────────────────

def test_trade_to_swap_fields():
    sw = _trade_to_swap(_raw("W1", "buy", 100.0, 2.0, 1234), "MINT")
    assert sw.side == "buy"
    assert sw.token_mint == "MINT"
    assert sw.sol_amount == 2.0
    assert sw.token_amount == 100.0
    assert sw.signer == "W1"
    assert sw.timestamp == 1234
    assert sw.slot == 1234   # slot ← time


def test_trade_to_swap_rejects_bad_side():
    assert _trade_to_swap({"wallet": "W1", "type": "unknown", "time": 1}, "MINT") is None


def test_trade_to_swap_rejects_missing_wallet():
    assert _trade_to_swap({"type": "buy", "time": 1}, "MINT") is None


# ── get_token_trades pagination (monkeypatch _get) ──────────────────────────────

class _FakeClient(SolanaTrackerClient):
    def __init__(self, pages):
        super().__init__(api_key="x")
        self._pages = pages
        self._calls = 0

    async def _get(self, path, params=None, retries=4):
        if path.startswith("/trades/"):
            i = 0 if (params or {}).get("cursor") is None else int(params["cursor"])
            return self._pages[i] if i < len(self._pages) else None
        return None


def _page(trades, cursor_next=None):
    return {"trades": trades, "hasNextPage": cursor_next is not None, "nextCursor": cursor_next}


@pytest.mark.asyncio
async def test_get_token_trades_walks_pages():
    pages = [
        _page([_raw("A", time=5000), _raw("B", time=4000)], cursor_next=1),
        _page([_raw("C", time=3000)], cursor_next=None),
    ]
    c = _FakeClient(pages)
    swaps = await c.get_token_trades("MINT")
    assert {s.signer for s in swaps} == {"A", "B", "C"}


@pytest.mark.asyncio
async def test_get_token_trades_since_ts_early_stop():
    pages = [
        _page([_raw("A", time=5000), _raw("B", time=4000)], cursor_next=1),
        _page([_raw("C", time=2000)], cursor_next=2),  # 2000 < since 3000 → drop + stop
        _page([_raw("D", time=1000)], cursor_next=None),
    ]
    c = _FakeClient(pages)
    swaps = await c.get_token_trades("MINT", since_ts=3000)
    sigs = {s.signer for s in swaps}
    assert sigs == {"A", "B"}        # C dropped (below since), D never fetched


@pytest.mark.asyncio
async def test_get_token_trades_respects_max_pages():
    pages = [_page([_raw("A", time=9000)], cursor_next=1),
             _page([_raw("B", time=8000)], cursor_next=2),
             _page([_raw("C", time=7000)], cursor_next=3)]
    c = _FakeClient(pages)
    swaps = await c.get_token_trades("MINT", max_pages=2)
    assert {s.signer for s in swaps} == {"A", "B"}


# ── get_token_holders shape ─────────────────────────────────────────────────────

class _HolderClient(SolanaTrackerClient):
    def __init__(self, payload):
        super().__init__(api_key="x")
        self._payload = payload

    async def _get(self, path, params=None, retries=4):
        return self._payload


@pytest.mark.asyncio
async def test_get_token_holders_shape():
    c = _HolderClient({"accounts": [
        {"wallet": "W1", "amount": 500.0},
        {"wallet": "W2", "balance": 250.0},
    ]})
    holders = await c.get_token_holders("MINT")
    assert holders == [{"address": "W1", "uiAmount": 500.0}, {"address": "W2", "uiAmount": 250.0}]
