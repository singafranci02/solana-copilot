"""Helius RPC client and transaction decoder.

Helius docs: https://docs.helius.dev
"""

import asyncio
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from src.common.config import settings
from src.common.models import TokenBuyer

PUMP_FUN_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
# TODO: replace with confirmed on-chain program ID once Bags is deployed to mainnet
BAGS_PROGRAM_ID = "BaGS111111111111111111111111111111111111111111"

_LAMPORTS_PER_SOL = 1_000_000_000
_METADATA_CACHE_TTL = 86_400  # 24 h in seconds


@dataclass
class Swap:
    """Canonical representation of a parsed Pump.fun or Bags swap."""

    side: str           # "buy" | "sell"
    token_mint: str
    sol_amount: float   # SOL, not lamports
    token_amount: float
    signer: str
    timestamp: int      # unix epoch
    slot: int


class _MetadataCache:
    """SQLite-backed 24-hour cache for Helius token-metadata responses."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._ensure_table()

    def _connect(self) -> sqlite3.Connection:
        path = Path(self._db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        return conn

    def _ensure_table(self) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS token_metadata_cache (
                    mint      TEXT PRIMARY KEY,
                    data      TEXT NOT NULL,
                    cached_at INTEGER NOT NULL
                )
                """
            )
            conn.commit()
        finally:
            conn.close()

    def get(self, mint: str) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT data, cached_at FROM token_metadata_cache WHERE mint = ?",
                (mint,),
            ).fetchone()
            if row and (time.time() - row["cached_at"]) < _METADATA_CACHE_TTL:
                return json.loads(row["data"])
            return None
        finally:
            conn.close()

    def set(self, mint: str, data: dict[str, Any]) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO token_metadata_cache (mint, data, cached_at)
                VALUES (?, ?, ?)
                """,
                (mint, json.dumps(data), int(time.time())),
            )
            conn.commit()
        finally:
            conn.close()


class HeliusClient:
    """Async wrapper around the Helius Enhanced Transactions API and RPC."""

    BASE_URL = "https://api.helius.xyz/v0"
    RPC_URL = "https://mainnet.helius-rpc.com"

    def __init__(
        self,
        api_key: str | None = None,
        requests_per_second: int = 10,
        db_path: str | None = None,
    ) -> None:
        self._api_key = api_key or settings.helius_api_key
        self._http = httpx.AsyncClient(timeout=30)
        self._semaphore = asyncio.Semaphore(requests_per_second)
        self._cache = _MetadataCache(db_path or settings.db_path)

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
        retries: int = 4,
    ) -> Any:
        query = {**(params or {}), "api-key": self._api_key}
        backoff = 1.0

        for attempt in range(retries):
            resp = None
            transport_error: Exception | None = None

            async with self._semaphore:
                try:
                    if method == "GET":
                        resp = await self._http.get(url, params=query)
                    else:
                        resp = await self._http.post(url, params=query, json=json_body)
                except httpx.TransportError as exc:
                    transport_error = exc

            if transport_error is not None:
                if attempt == retries - 1:
                    raise transport_error
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
                continue

            assert resp is not None

            if resp.status_code in (429, 503):
                retry_after = float(resp.headers.get("Retry-After", backoff))
                if attempt == retries - 1:
                    resp.raise_for_status()
                await asyncio.sleep(retry_after)
                backoff = min(backoff * 2, 30)
                continue

            resp.raise_for_status()
            return resp.json()

        raise RuntimeError("request failed after all retries")

    async def get_transactions_for_address(
        self,
        address: str,
        limit: int = 100,
        before: str | None = None,
        until: str | None = None,
        tx_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return full Helius-enhanced transactions for an address.

        before:  signature cursor — return txs older than this signature (pagination).
        until:   signature — stop at this signature.
        tx_type: Helius enhanced type filter, e.g. "SWAP".
        """
        params: dict[str, Any] = {"limit": limit}
        if before:
            params["before"] = before
        if until:
            params["until"] = until
        if tx_type:
            params["type"] = tx_type
        return await self._request(
            "GET",
            f"{self.BASE_URL}/addresses/{address}/transactions",
            params=params,
        )

    async def get_signatures_for_address(
        self, address: str, limit: int = 100
    ) -> list[str]:
        """Return recent transaction signatures for a wallet address."""
        data = await self.get_transactions_for_address(address, limit=limit)
        return [tx["signature"] for tx in data]

    async def get_transaction(self, signature: str) -> dict[str, Any]:
        """Fetch a single Helius-enhanced transaction by signature."""
        data = await self._request(
            "POST",
            f"{self.BASE_URL}/transactions",
            json_body={"transactions": [signature]},
        )
        if isinstance(data, list) and data:
            return data[0]
        return data

    async def get_token_largest_accounts(self, mint: str) -> list[dict[str, Any]]:
        """Return the top token accounts by balance for a mint (Solana JSON-RPC)."""
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTokenLargestAccounts",
            "params": [mint, {"commitment": "finalized"}],
        }
        data = await self._request("POST", f"{self.RPC_URL}/", json_body=payload)
        return (data.get("result") or {}).get("value", [])

    async def get_token_metadata(self, mint: str) -> dict[str, Any]:
        """Fetch DAS asset metadata for a mint address; results cached for 24 h."""
        cached = self._cache.get(mint)
        if cached is not None:
            return cached

        data = await self._request(
            "POST",
            f"{self.BASE_URL}/token-metadata",
            json_body={"mintAccounts": [mint]},
        )
        result: dict[str, Any] = data[0] if isinstance(data, list) else data
        self._cache.set(mint, result)
        return result

    async def close(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "HeliusClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()


# ── swap parsing ──────────────────────────────────────────────────────────────

def parse_swap(raw_tx: dict[str, Any]) -> Swap | None:
    """Extract a canonical Swap from a Helius enhanced transaction.

    Recognises Pump.fun (source=="PUMP_FUN" or program 6EF8…) and
    Bags (source=="BAGS" or BAGS_PROGRAM_ID in instructions).
    Returns None for any transaction that doesn't match.
    """
    source = raw_tx.get("source", "")

    program_ids: set[str] = set()
    for inst in raw_tx.get("instructions", []):
        program_ids.add(inst.get("programId", ""))
        for inner in inst.get("innerInstructions", []):
            program_ids.add(inner.get("programId", ""))

    is_pump = source == "PUMP_FUN" or PUMP_FUN_PROGRAM_ID in program_ids
    is_bags = source == "BAGS" or BAGS_PROGRAM_ID in program_ids

    if not (is_pump or is_bags):
        return None

    signer: str = raw_tx.get("feePayer", "")
    timestamp: int = raw_tx.get("timestamp", 0)
    slot: int = raw_tx.get("slot", 0)

    token_mint: str | None = None
    token_amount: float = 0.0
    side: str | None = None

    for tf in raw_tx.get("tokenTransfers", []):
        mint = tf.get("mint", "")
        if not mint:
            continue
        decimals = tf.get("decimals", 0)
        raw_amount = tf.get("tokenAmount", 0)
        ui_amount = raw_amount / (10 ** decimals) if decimals else float(raw_amount)

        if tf.get("toUserAccount") == signer:
            side = "buy"
            token_mint = mint
            token_amount = ui_amount
            break
        if tf.get("fromUserAccount") == signer:
            side = "sell"
            token_mint = mint
            token_amount = ui_amount
            break

    if token_mint is None or side is None:
        return None

    sol_amount = 0.0
    for nt in raw_tx.get("nativeTransfers", []):
        lamports = nt.get("amount", 0)
        if side == "buy" and nt.get("fromUserAccount") == signer:
            sol_amount += lamports / _LAMPORTS_PER_SOL
        elif side == "sell" and nt.get("toUserAccount") == signer:
            sol_amount += lamports / _LAMPORTS_PER_SOL

    return Swap(
        side=side,
        token_mint=token_mint,
        sol_amount=sol_amount,
        token_amount=token_amount,
        signer=signer,
        timestamp=timestamp,
        slot=slot,
    )


async def paginate_address_txs(
    client,
    address: str,
    *,
    until_ts: int,
    max_pages: int = 50,
    tx_type: str | None = None,
) -> list[dict[str, Any]]:
    """Walk an address's tx history backward until until_ts, empty, or max_pages.

    Uses the `before` signature cursor. `client` is any object exposing
    get_transactions_for_address(address, limit, before, tx_type) — so this is
    unit-testable with a mock. Returns raw enhanced txs with timestamp >= until_ts.
    """
    out: list[dict[str, Any]] = []
    before: str | None = None
    for _ in range(max_pages):
        page = await client.get_transactions_for_address(
            address, limit=100, before=before, tx_type=tx_type,
        )
        if not page:
            break
        for tx in page:
            if int(tx.get("timestamp", 0)) >= until_ts:
                out.append(tx)
        oldest_ts = int(page[-1].get("timestamp", 0))
        if oldest_ts < until_ts:
            break
        next_before = page[-1].get("signature")
        if not next_before or next_before == before:
            break
        before = next_before
    return out


# ── backward-compat helpers (used by existing callers / smoke tests) ──────────

CEX_HOT_WALLETS: frozenset[str] = frozenset(
    {
        "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi",  # Coinbase
        "2ojv9BAiHUrvsm9gxDe7fJSzbNZSJcxZvf8dqmWGHG8S",  # Binance
        "AC5RDfQFmDS1deWZos921JfqscXdByf8BrmHaL2HPFL",   # OKX
    }
)
# backward-compat alias kept for any code that imported the private name
_CEX_HOT_WALLETS = CEX_HOT_WALLETS


def decode_swap_transaction(raw_tx: dict[str, Any]) -> TokenBuyer | None:
    """Extract a TokenBuyer from a Helius-parsed swap transaction.

    Returns None for sells or unrecognised transactions.
    """
    swap = parse_swap(raw_tx)
    if swap is None or swap.side != "buy":
        return None
    return TokenBuyer(
        token_mint=swap.token_mint,
        wallet_address=swap.signer,
        bought_at=swap.timestamp,
        sol_amount=swap.sol_amount,
        tokens_received=swap.token_amount,
    )


def extract_funding_source(wallet_txs: list[dict[str, Any]]) -> str | None:
    """Walk a wallet's transaction history (oldest-first) to find its SOL funder.

    Returns "cex" if the funding wallet is a known CEX hot wallet, the funder
    address otherwise, or None if no clear funding source is found.
    """
    for tx in wallet_txs:
        fee_payer = tx.get("feePayer", "")
        for nt in tx.get("nativeTransfers", []):
            sender = nt.get("fromUserAccount", "")
            if not sender or sender == fee_payer:
                continue
            if sender in _CEX_HOT_WALLETS:
                return "cex"
            return sender
    return None
