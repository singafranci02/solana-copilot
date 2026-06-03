"""Minimal Solana JSON-RPC client — used ONLY for funding-source tracing.

Solana Tracker can't tell you who first sent a wallet its SOL, and Helius is retired
here. A free RPC (Alchemy/Ankr via RPC_URL) covers the two cheap standard methods we
need: getSignaturesForAddress + getTransaction(jsonParsed). Low volume — only the few
significant wallets per graduation, gated by the caller.
"""

import asyncio
import logging
from typing import Any

import aiohttp

from src.common.config import settings
from src.ingest.helius import CEX_HOT_WALLETS

logger = logging.getLogger(__name__)


class RpcClient:
    def __init__(self, url: str | None = None, requests_per_second: int = 8) -> None:
        self._url = url or settings.rpc_url
        self._session: aiohttp.ClientSession | None = None
        self._semaphore = asyncio.Semaphore(requests_per_second)
        self._id = 0

    async def __aenter__(self) -> "RpcClient":
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._session:
            await self._session.close()

    async def _call(self, method: str, params: list, retries: int = 3) -> Any:
        assert self._session is not None and self._url, "RPC_URL not configured"
        self._id += 1
        body = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}
        backoff = 1.0
        for attempt in range(retries):
            async with self._semaphore:
                try:
                    async with self._session.post(self._url, json=body) as resp:
                        if resp.status in (429, 503):
                            if attempt == retries - 1:
                                resp.raise_for_status()
                            await asyncio.sleep(backoff)
                            backoff = min(backoff * 2, 20)
                            continue
                        resp.raise_for_status()
                        data = await resp.json()
                        return data.get("result")
                except aiohttp.ClientError:
                    if attempt == retries - 1:
                        raise
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 20)
        return None

    async def get_signatures_for_address(self, address: str, limit: int = 1000) -> list[dict]:
        return await self._call(
            "getSignaturesForAddress", [address, {"limit": limit}]
        ) or []

    async def get_transaction(self, signature: str) -> dict | None:
        return await self._call(
            "getTransaction",
            [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
        )


def _funder_from_tx(tx: dict, wallet: str) -> str | None:
    """Find a SOL transfer into `wallet` in a jsonParsed tx → the sender."""
    try:
        instrs = tx["transaction"]["message"]["instructions"]
    except (KeyError, TypeError):
        return None
    for ins in instrs:
        parsed = ins.get("parsed") if isinstance(ins, dict) else None
        if not isinstance(parsed, dict):
            continue
        if parsed.get("type") != "transfer":
            continue
        info = parsed.get("info") or {}
        if info.get("destination") == wallet:
            src = info.get("source")
            if src and src != wallet:
                return "cex" if src in CEX_HOT_WALLETS else src
    return None


async def extract_funding_source_rpc(client: RpcClient, wallet: str, scan: int = 5) -> str | None:
    """Walk a wallet's OLDEST transactions to find its first SOL funder.

    getSignaturesForAddress returns newest-first; the funding tx is the oldest, so we
    scan from the end. Fresh team wallets have few txs, so one page suffices.
    """
    sigs = await client.get_signatures_for_address(wallet, limit=1000)
    if not sigs:
        return None
    oldest = list(reversed(sigs))[:scan]   # oldest-first, capped
    for s in oldest:
        sig = s.get("signature")
        if not sig:
            continue
        tx = await client.get_transaction(sig)
        if not tx:
            continue
        funder = _funder_from_tx(tx, wallet)
        if funder:
            return funder
    return None
