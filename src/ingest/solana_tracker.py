"""Solana Tracker Data API client — primary trades + holders source.

Replaces the Helius enhanced-transaction API (free tier exhausted). Solana Tracker
returns a token's full trade history BY MINT in one cursor-paginated sequence, which
collapses all the old per-wallet Helius fan-out into a single call per token.

Base: https://data.solanatracker.io  | auth header: x-api-key  | 60 RPS (Developer).

Trades carry NO slot — we map `time` (unix seconds) into Swap.slot so the coordination
engine's same-slot bundling becomes same-SECOND bundling (~400ms slots → same second ≈
same/adjacent block, a sound bundle proxy). See coordination.group_by_slot.
"""

import asyncio
import logging
from typing import Any

import aiohttp

from src.common.config import settings
from src.ingest.helius import Swap

logger = logging.getLogger(__name__)

_BASE_URL = "https://data.solanatracker.io"


class SolanaTrackerClient:
    """Async client for the Solana Tracker Data API."""

    def __init__(self, api_key: str | None = None, requests_per_second: int = 8) -> None:
        self._api_key = api_key or settings.solana_tracker_api_key
        self._session: aiohttp.ClientSession | None = None
        self._semaphore = asyncio.Semaphore(requests_per_second)

    async def __aenter__(self) -> "SolanaTrackerClient":
        self._session = aiohttp.ClientSession(
            headers={"x-api-key": self._api_key},
            timeout=aiohttp.ClientTimeout(total=30),
        )
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._session:
            await self._session.close()

    async def _get(self, path: str, params: dict | None = None, retries: int = 4) -> Any:
        assert self._session is not None, "use as async context manager"
        url = f"{_BASE_URL}{path}"
        backoff = 1.0
        for attempt in range(retries):
            async with self._semaphore:
                try:
                    async with self._session.get(url, params=params or {}) as resp:
                        if resp.status in (429, 503):
                            if attempt == retries - 1:
                                resp.raise_for_status()
                            await asyncio.sleep(float(resp.headers.get("Retry-After", backoff)))
                            backoff = min(backoff * 2, 30)
                            continue
                        resp.raise_for_status()
                        return await resp.json()
                except aiohttp.ClientError:
                    if attempt == retries - 1:
                        raise
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30)
        return None

    # ── trades ──────────────────────────────────────────────────────────────────

    async def get_token_trades(
        self,
        mint: str,
        since_ts: int | None = None,
        max_pages: int = 50,
        sort: str = "DESC",
    ) -> list[Swap]:
        """Full trade history for a mint, normalized to Swap. Walks the cursor.

        DESC order (newest first) + since_ts → early-stop once a page's oldest trade
        predates since_ts. Returns trades with timestamp >= since_ts (if given).
        """
        out: list[Swap] = []
        cursor: Any = None
        for _ in range(max_pages):
            params: dict[str, Any] = {"sortDirection": sort}
            if cursor is not None:
                params["cursor"] = cursor
            data = await self._get(f"/trades/{mint}", params)
            if not data:
                break
            trades = data.get("trades") or []
            if not trades:
                break
            page_swaps = [s for s in (_trade_to_swap(t, mint) for t in trades) if s]
            for sw in page_swaps:
                if since_ts is not None and sw.timestamp < since_ts:
                    continue
                out.append(sw)
            # early-stop: oldest trade on this DESC page is already before the window
            oldest = min((s.timestamp for s in page_swaps), default=0)
            if since_ts is not None and sort == "DESC" and oldest < since_ts:
                break
            if not data.get("hasNextPage"):
                break
            cursor = data.get("nextCursor")
            if cursor is None:
                break
        return out

    # ── holders ─────────────────────────────────────────────────────────────────

    async def get_token_holders(self, mint: str, limit: int = 100) -> list[dict]:
        """Top holders as [{address, uiAmount}] — matches the old Helius shape so
        _parse_bc_holders / compute_holder_snapshot need no change."""
        data = await self._get(f"/tokens/{mint}/holders", {"limit": limit})
        if not data:
            return []
        # ST may return {"accounts":[...]} or a bare list, each {wallet, amount/balance, ...}
        rows = data.get("accounts") if isinstance(data, dict) else data
        out: list[dict] = []
        for r in rows or []:
            addr = r.get("wallet") or r.get("address") or r.get("owner")
            amt = r.get("amount")
            if amt is None:
                amt = r.get("balance") or r.get("uiAmount")
            if addr and amt is not None:
                out.append({"address": addr, "uiAmount": float(amt)})
        return out

    async def get_price(self, mint: str) -> dict | None:
        """Current price/liquidity/marketCap (DexScreener stays primary; this is a fallback)."""
        return await self._get("/price", {"token": mint})


# ── normalization ───────────────────────────────────────────────────────────────

def _trade_to_swap(t: dict, mint: str) -> Swap | None:
    """Map a Solana Tracker trade to the canonical Swap (slot ← time seconds)."""
    side = t.get("type")
    wallet = t.get("wallet")
    if side not in ("buy", "sell") or not wallet:
        return None
    # Solana Tracker returns `time` in MILLISECONDS; the rest of the system uses
    # unix SECONDS (graduated_at, since_ts, lockstep windows, same-second bundling).
    raw = int(t.get("time", 0))
    ts = raw // 1000 if raw > 10_000_000_000 else raw   # ms→s (guard if ever seconds)
    return Swap(
        side=side,
        token_mint=mint,
        sol_amount=float(t.get("volumeSol") or 0),
        token_amount=float(t.get("amount") or 0),
        signer=wallet,
        timestamp=ts,
        slot=ts,                      # no slot from ST → second-granularity bundle key
    )
