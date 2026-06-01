"""Tests for src/ingest/helius.py.

All HTTP calls are mocked — no real Helius API is contacted.
Covers one Pump.fun swap (buy) and one Bags swap (sell).
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from src.ingest.helius import (
    BAGS_PROGRAM_ID,
    PUMP_FUN_PROGRAM_ID,
    HeliusClient,
    Swap,
    decode_swap_transaction,
    parse_swap,
)

# ── shared fixtures ────────────────────────────────────────────────────────────

SIGNER = "3XjWBtocBpjFimcYDDeKtnCeABC12345678901234567"
TOKEN_MINT_PUMP = "PuMpXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"
TOKEN_MINT_BAGS = "BaGsXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"

# Helius enhanced transaction: Pump.fun buy — signer pays 0.5 SOL, receives 1 000 tokens
PUMP_FUN_BUY_TX: dict = {
    "description": "Swap 0.5 SOL for 1000 MEME on Pump.fun",
    "type": "SWAP",
    "source": "PUMP_FUN",
    "fee": 5000,
    "feePayer": SIGNER,
    "signature": "pumpSig1111111111111111111111111111111111111111",
    "slot": 300_000_001,
    "timestamp": 1_716_000_000,
    "tokenTransfers": [
        {
            "fromUserAccount": PUMP_FUN_PROGRAM_ID,
            "toUserAccount": SIGNER,
            "fromTokenAccount": "fromTA1",
            "toTokenAccount": "toTA1",
            "tokenAmount": 1_000_000_000,  # 6 decimals → 1 000.0
            "decimals": 6,
            "mint": TOKEN_MINT_PUMP,
            "tokenStandard": "Fungible",
        }
    ],
    "nativeTransfers": [
        {
            "fromUserAccount": SIGNER,
            "toUserAccount": PUMP_FUN_PROGRAM_ID,
            "amount": 500_000_000,  # 0.5 SOL in lamports
        }
    ],
    "instructions": [
        {"programId": PUMP_FUN_PROGRAM_ID, "accounts": [], "data": ""},
    ],
    "accountData": [],
}

# Helius enhanced transaction: Bags sell — signer sends 500 tokens, receives 0.25 SOL
BAGS_SELL_TX: dict = {
    "description": "Swap 500 BAGS_TOKEN for 0.25 SOL on Bags",
    "type": "SWAP",
    "source": "BAGS",
    "fee": 5000,
    "feePayer": SIGNER,
    "signature": "bagsSig1111111111111111111111111111111111111111",
    "slot": 300_000_002,
    "timestamp": 1_716_001_000,
    "tokenTransfers": [
        {
            "fromUserAccount": SIGNER,
            "toUserAccount": BAGS_PROGRAM_ID,
            "fromTokenAccount": "fromTA2",
            "toTokenAccount": "toTA2",
            "tokenAmount": 500_000_000_000,  # 9 decimals → 500.0
            "decimals": 9,
            "mint": TOKEN_MINT_BAGS,
            "tokenStandard": "Fungible",
        }
    ],
    "nativeTransfers": [
        {
            "fromUserAccount": BAGS_PROGRAM_ID,
            "toUserAccount": SIGNER,
            "amount": 250_000_000,  # 0.25 SOL in lamports
        }
    ],
    "instructions": [
        {"programId": BAGS_PROGRAM_ID, "accounts": [], "data": ""},
    ],
    "accountData": [],
}


def _mock_resp(data, status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.headers = {}
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=data)
    return resp


@pytest.fixture
def client(tmp_path):
    """HeliusClient with a temp SQLite cache and a mocked httpx transport."""
    c = HeliusClient(api_key="test-key", db_path=str(tmp_path / "test.db"))
    c._http = MagicMock()
    return c


# ── parse_swap: pure unit tests (no I/O) ──────────────────────────────────────

def test_parse_swap_pump_fun_buy():
    swap = parse_swap(PUMP_FUN_BUY_TX)

    assert isinstance(swap, Swap)
    assert swap.side == "buy"
    assert swap.token_mint == TOKEN_MINT_PUMP
    assert swap.sol_amount == pytest.approx(0.5)
    assert swap.token_amount == pytest.approx(1_000.0)
    assert swap.signer == SIGNER
    assert swap.timestamp == 1_716_000_000
    assert swap.slot == 300_000_001


def test_parse_swap_bags_sell():
    swap = parse_swap(BAGS_SELL_TX)

    assert isinstance(swap, Swap)
    assert swap.side == "sell"
    assert swap.token_mint == TOKEN_MINT_BAGS
    assert swap.sol_amount == pytest.approx(0.25)
    assert swap.token_amount == pytest.approx(500.0)
    assert swap.signer == SIGNER
    assert swap.timestamp == 1_716_001_000
    assert swap.slot == 300_000_002


def test_parse_swap_non_swap_returns_none():
    # Not a swap: non-DEX source, no SWAP type, unknown program → None
    tx = {
        **PUMP_FUN_BUY_TX,
        "type": "TRANSFER",
        "source": "SYSTEM_PROGRAM",
        "instructions": [{"programId": "SomeOtherProgram1111111111111111111111"}],
    }
    assert parse_swap(tx) is None


def test_parse_swap_recognises_pumpswap_amm():
    # Post-graduation swaps happen on PumpSwap AMM — must be recognised
    tx = {
        **PUMP_FUN_BUY_TX,
        "type": "SWAP",
        "source": "PUMP_AMM",
        "instructions": [{"programId": "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"}],
    }
    swap = parse_swap(tx)
    assert swap is not None
    assert swap.side == "buy"


def test_parse_swap_detected_via_program_id_fallback():
    # source is blank but instruction contains the Pump.fun program ID
    tx = {**PUMP_FUN_BUY_TX, "source": "UNKNOWN"}
    swap = parse_swap(tx)
    assert swap is not None
    assert swap.side == "buy"


def test_parse_swap_no_token_transfers_returns_none():
    tx = {**PUMP_FUN_BUY_TX, "tokenTransfers": []}
    assert parse_swap(tx) is None


# ── decode_swap_transaction backward-compat ────────────────────────────────────

def test_decode_swap_transaction_buy_returns_token_buyer():
    tb = decode_swap_transaction(PUMP_FUN_BUY_TX)

    assert tb is not None
    assert tb.token_mint == TOKEN_MINT_PUMP
    assert tb.wallet_address == SIGNER
    assert tb.sol_amount == pytest.approx(0.5)
    assert tb.tokens_received == pytest.approx(1_000.0)
    assert tb.bought_at == 1_716_000_000


def test_decode_swap_transaction_sell_returns_none():
    assert decode_swap_transaction(BAGS_SELL_TX) is None


# ── HeliusClient HTTP methods (mocked httpx) ──────────────────────────────────

async def test_get_transaction_pump_fun(client):
    client._http.post = AsyncMock(return_value=_mock_resp([PUMP_FUN_BUY_TX]))

    tx = await client.get_transaction("pumpSig1")

    assert tx["signature"].startswith("pumpSig")
    assert tx["source"] == "PUMP_FUN"
    swap = parse_swap(tx)
    assert swap is not None
    assert swap.side == "buy"
    assert swap.sol_amount == pytest.approx(0.5)


async def test_get_transaction_bags(client):
    client._http.post = AsyncMock(return_value=_mock_resp([BAGS_SELL_TX]))

    tx = await client.get_transaction("bagsSig1")

    assert tx["source"] == "BAGS"
    swap = parse_swap(tx)
    assert swap is not None
    assert swap.side == "sell"
    assert swap.sol_amount == pytest.approx(0.25)
    assert swap.token_amount == pytest.approx(500.0)


async def test_get_signatures_for_address(client):
    txns = [
        {"signature": "sig111", "type": "SWAP"},
        {"signature": "sig222", "type": "TRANSFER"},
    ]
    client._http.get = AsyncMock(return_value=_mock_resp(txns))

    sigs = await client.get_signatures_for_address("walletABC", limit=2)

    assert sigs == ["sig111", "sig222"]
    client._http.get.assert_called_once()


async def test_get_token_largest_accounts(client):
    rpc_response = {
        "jsonrpc": "2.0",
        "result": {
            "context": {"slot": 300_000_000},
            "value": [
                {"address": "acc1", "amount": "1000000", "decimals": 6, "uiAmount": 1.0},
                {"address": "acc2", "amount": "500000", "decimals": 6, "uiAmount": 0.5},
            ],
        },
        "id": 1,
    }
    client._http.post = AsyncMock(return_value=_mock_resp(rpc_response))

    accounts = await client.get_token_largest_accounts(TOKEN_MINT_PUMP)

    assert len(accounts) == 2
    assert accounts[0]["address"] == "acc1"


async def test_get_token_metadata_cache_miss_then_hit(client):
    meta = {"mint": TOKEN_MINT_PUMP, "name": "Meme Token", "symbol": "MEME"}
    client._http.post = AsyncMock(return_value=_mock_resp([meta]))

    # First call: cache miss → HTTP request
    result1 = await client.get_token_metadata(TOKEN_MINT_PUMP)
    assert result1["symbol"] == "MEME"
    assert client._http.post.call_count == 1

    # Second call: cache hit → no additional HTTP request
    result2 = await client.get_token_metadata(TOKEN_MINT_PUMP)
    assert result2["symbol"] == "MEME"
    assert client._http.post.call_count == 1  # unchanged


async def test_retry_on_429(client):
    rate_limited = _mock_resp(None, status=429)
    rate_limited.headers = {"Retry-After": "0"}
    ok = _mock_resp([PUMP_FUN_BUY_TX])

    client._http.post = AsyncMock(side_effect=[rate_limited, ok])

    tx = await client.get_transaction("pumpSig1")

    assert tx["source"] == "PUMP_FUN"
    assert client._http.post.call_count == 2


async def test_retry_on_503(client):
    unavailable = _mock_resp(None, status=503)
    unavailable.headers = {}
    ok = _mock_resp([BAGS_SELL_TX])

    client._http.post = AsyncMock(side_effect=[unavailable, ok])

    tx = await client.get_transaction("bagsSig1")

    assert tx["source"] == "BAGS"
    assert client._http.post.call_count == 2
