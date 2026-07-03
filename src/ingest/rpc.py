"""Minimal Solana JSON-RPC client — used ONLY for funding-source tracing.

Solana Tracker can't tell you who first sent a wallet its SOL, and Helius is retired
here. A free RPC (Alchemy/Ankr via RPC_URL) covers the two cheap standard methods we
need: getSignaturesForAddress + getTransaction(jsonParsed). Low volume — only the few
significant wallets per graduation, gated by the caller.
"""

import asyncio
import logging
from dataclasses import dataclass
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
        from src.common.api_usage import record
        self._id += 1
        body = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}
        backoff = 1.0
        for attempt in range(retries):
            record("rpc", method)
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


@dataclass
class FundingInfo:
    """First-funder trace for a wallet, plus freshness signals from the same
    getSignaturesForAddress call (no extra requests)."""

    wallet: str
    funder: str | None = None          # "cex" or funder address
    lamports: int | None = None
    funded_at: int | None = None       # blockTime of the funding tx
    tx_signature: str | None = None
    first_seen: int | None = None      # oldest blockTime observed for the wallet
    sig_count: int = 0                 # signatures returned (freshness proxy, ≤1000)


# Instruction types that move SOL into a fresh wallet
_FUNDING_TYPES = ("transfer", "transferChecked", "createAccount", "createAccountWithSeed")


def _iter_parsed_instructions(tx: dict):
    """Yield parsed instruction dicts from top-level AND inner instructions."""
    try:
        yield from tx["transaction"]["message"]["instructions"]
    except (KeyError, TypeError):
        pass
    for group in (tx.get("meta") or {}).get("innerInstructions") or []:
        yield from group.get("instructions") or []


def _funder_from_tx(tx: dict, wallet: str) -> tuple[str, int | None] | None:
    """Find a SOL transfer/account-creation into `wallet` → (sender, lamports)."""
    for ins in _iter_parsed_instructions(tx):
        parsed = ins.get("parsed") if isinstance(ins, dict) else None
        if not isinstance(parsed, dict) or parsed.get("type") not in _FUNDING_TYPES:
            continue
        info = parsed.get("info") or {}
        dest = info.get("destination") or info.get("newAccount")
        if dest != wallet:
            continue
        src = info.get("source")
        if src and src != wallet:
            lamports = info.get("lamports")
            try:
                lamports = int(lamports) if lamports is not None else None
            except (TypeError, ValueError):
                lamports = None
            return ("cex" if src in CEX_HOT_WALLETS else src), lamports
    return None


async def extract_funding_info_rpc(
    client: RpcClient, wallet: str, scan: int = 5, fresh_gate: int | None = None
) -> FundingInfo:
    """Walk a wallet's OLDEST transactions for its first SOL funder + wallet age.

    getSignaturesForAddress returns newest-first; the funding tx is the oldest,
    so we scan from the end. Fresh team wallets have few txs, so one page
    suffices. first_seen/sig_count come from the same response for free.

    fresh_gate: skip the (expensive) tx scan when the wallet has at least this
    many signatures — used for hop-2 tracing, where only fresh intermediary
    funders are worth peeling.
    """
    info = FundingInfo(wallet=wallet)
    sigs = await client.get_signatures_for_address(wallet, limit=1000)
    if not sigs:
        return info
    info.sig_count = len(sigs)
    if fresh_gate is not None and info.sig_count >= fresh_gate:
        for s in reversed(sigs):
            if s.get("blockTime"):
                info.first_seen = int(s["blockTime"])
                break
        return info
    oldest_first = list(reversed(sigs))
    for s in oldest_first:
        if s.get("blockTime"):
            info.first_seen = int(s["blockTime"])
            break
    for s in oldest_first[:scan]:
        sig = s.get("signature")
        if not sig:
            continue
        tx = await client.get_transaction(sig)
        if not tx:
            continue
        found = _funder_from_tx(tx, wallet)
        if found:
            info.funder, info.lamports = found
            info.funded_at = s.get("blockTime")
            info.tx_signature = sig
            return info
    return info


async def extract_funding_source_rpc(client: RpcClient, wallet: str, scan: int = 5) -> str | None:
    """Back-compat wrapper: just the funder address (or 'cex'), or None."""
    return (await extract_funding_info_rpc(client, wallet, scan)).funder
