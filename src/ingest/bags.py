"""Bags launchpad REST API client."""

from typing import Any

import httpx

from src.common.config import settings
from src.common.models import Token, TokenBuyer


class BagsClient:
    """Async client for the Bags REST API."""

    BASE_URL = "https://api.bags.fm/v1"

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or settings.bags_api_key
        self._http = httpx.AsyncClient(
            timeout=30,
            headers={"X-API-Key": self._api_key} if self._api_key else {},
        )

    async def _get(self, path: str, **params: Any) -> Any:
        resp = await self._http.get(f"{self.BASE_URL}{path}", params=params or {})
        resp.raise_for_status()
        return resp.json()

    async def get_token(self, mint: str) -> dict[str, Any]:
        """Fetch Bags-specific token data (curve state, graduated status, etc.)."""
        return await self._get(f"/token/{mint}")

    async def get_token_trades(
        self, mint: str, limit: int = 200
    ) -> list[dict[str, Any]]:
        """Fetch recent trades on a Bags token."""
        data = await self._get(f"/token/{mint}/trades", limit=limit)
        if isinstance(data, dict):
            return data.get("data") or data.get("trades") or []
        return data or []

    async def get_launchpad_feed(self, limit: int = 50) -> list[dict[str, Any]]:
        """Fetch the latest tokens launched on Bags."""
        data = await self._get("/feed", limit=limit)
        if isinstance(data, dict):
            return data.get("data") or data.get("tokens") or []
        return data or []

    async def close(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "BagsClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()


def parse_bags_token(raw: dict[str, Any]) -> Token:
    """Map a Bags token dict to a Token dataclass."""
    mint = raw.get("mint") or raw.get("address") or ""
    symbol = raw.get("symbol") or raw.get("ticker") or ""
    name = raw.get("name") or symbol
    created_at = int(raw.get("created_at") or raw.get("launchTimestamp") or 0)

    top10_raw = raw.get("top10HolderRate") or raw.get("top10_holder_rate")
    if top10_raw is not None:
        top10_f = float(top10_raw)
        top10_pct: float | None = top10_f if top10_f > 1 else top10_f * 100
    else:
        top10_pct = None

    return Token(
        mint=mint,
        symbol=symbol,
        name=name,
        launchpad="bags",
        created_at=created_at,
        market_cap_usd_snapshot=raw.get("marketCap") or raw.get("market_cap"),
        holders_count_snapshot=raw.get("holderCount") or raw.get("holder_count"),
        lp_burned=bool(raw.get("lpBurned") or raw.get("lp_burned")),
        top10_pct=top10_pct,
    )


def parse_bags_trade(raw: dict[str, Any]) -> TokenBuyer:
    """Map a Bags trade dict to a TokenBuyer dataclass."""
    return TokenBuyer(
        token_mint=raw.get("mint") or raw.get("tokenMint") or "",
        wallet_address=raw.get("wallet") or raw.get("trader") or "",
        bought_at=int(raw.get("timestamp") or raw.get("ts") or 0),
        sol_amount=float(raw.get("solAmount") or raw.get("sol_amount") or 0.0),
        tokens_received=float(raw.get("tokenAmount") or raw.get("tokens") or 0.0),
    )
