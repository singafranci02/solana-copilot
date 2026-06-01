"""Tests for live_trades dedup/price + Helius pagination walker (offline)."""

import pytest

from src.analyzer.live_trades import LiveTrade, dedup_key, price_sol
from src.ingest.helius import paginate_address_txs


def _t(**kw):
    base = dict(
        token_mint="MINT", wallet_address="W1", side="buy",
        sol_amount=1.0, token_amount=100.0, ts=1000,
    )
    base.update(kw)
    return LiveTrade(**base)


# ── dedup_key ───────────────────────────────────────────────────────────────────

def test_dedup_key_uses_signature_when_present():
    t = _t(signature="SIG123")
    assert dedup_key(t) == "SIG123:W1"


def test_dedup_key_composite_when_no_signature():
    t = _t()
    assert dedup_key(t) == "MINT:W1:1000:buy:1.0"


def test_identical_signatureless_trades_same_key():
    assert dedup_key(_t()) == dedup_key(_t())


def test_opposite_sides_distinct_keys():
    assert dedup_key(_t(side="buy")) != dedup_key(_t(side="sell"))


def test_different_wallets_distinct_keys():
    assert dedup_key(_t(wallet_address="W1")) != dedup_key(_t(wallet_address="W2"))


# ── price_sol ─────────────────────────────────────────────────────────────────

def test_price_sol_normal():
    assert price_sol(_t(sol_amount=2.0, token_amount=100.0)) == 0.02


def test_price_sol_zero_tokens_none():
    assert price_sol(_t(token_amount=0)) is None


# ── pagination walker ───────────────────────────────────────────────────────────

class _MockClient:
    """Returns canned pages keyed by the `before` cursor."""
    def __init__(self, pages):
        self.pages = pages           # list of pages, each list of tx dicts
        self.calls = []

    async def get_transactions_for_address(self, address, limit=100, before=None, tx_type=None):
        self.calls.append(before)
        # page 0 when before is None, then follow signatures
        if before is None:
            return self.pages[0]
        for i, page in enumerate(self.pages):
            if page and page[-1]["signature"] == before and i + 1 < len(self.pages):
                return self.pages[i + 1]
        return []


def _tx(sig, ts):
    return {"signature": sig, "timestamp": ts}


@pytest.mark.asyncio
async def test_pagination_walks_until_timestamp():
    pages = [
        [_tx("a", 5000), _tx("b", 4000)],
        [_tx("c", 3000), _tx("d", 2000)],   # d=2000 < until 2500 → stop after this page
        [_tx("e", 1000)],
    ]
    client = _MockClient(pages)
    out = await paginate_address_txs(client, "POOL", until_ts=2500, max_pages=10)
    sigs = {t["signature"] for t in out}
    # keeps txs >= 2500: a,b,c (d=2000 excluded), stops before page 3
    assert sigs == {"a", "b", "c"}


@pytest.mark.asyncio
async def test_pagination_stops_on_empty_page():
    pages = [[_tx("a", 5000)], []]
    client = _MockClient(pages)
    out = await paginate_address_txs(client, "POOL", until_ts=0, max_pages=10)
    assert {t["signature"] for t in out} == {"a"}


@pytest.mark.asyncio
async def test_pagination_respects_max_pages():
    # all txs above until_ts so only max_pages limits it
    pages = [[_tx("a", 9000)], [_tx("b", 8000)], [_tx("c", 7000)]]
    client = _MockClient(pages)
    out = await paginate_address_txs(client, "POOL", until_ts=0, max_pages=2)
    # only 2 pages fetched
    assert len(client.calls) == 2
    assert {t["signature"] for t in out} == {"a", "b"}
