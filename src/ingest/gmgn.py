"""GMGN REST API client for token and wallet enrichment data.

Endpoints (base: https://gmgn.ai/api/v1):
  GET /smartmoney/sol/walletNew/{address}?period=30d  — wallet stats
  GET /token/sol/{mint}                               — token profile
  GET /token/sol/top_traders/{mint}                   — top traders by PnL

All responses are wrapped: {"code": 0, "msg": "ok", "data": {...}}.
A non-zero code is treated as an application-level error and raised.

NOTE: GMGN's API is partially undocumented.  Field names below match the
observable responses as of early 2024; verify against live responses and
update parse_* if GMGN changes field names.
"""

import asyncio
from typing import Any

import httpx

from src.common.config import settings
from src.common.models import Token, Wallet


class GMGNClient:
    """Async client for the GMGN REST API."""

    BASE_URL = "https://gmgn.ai/api/v1"

    def __init__(
        self,
        api_key: str | None = None,
        requests_per_second: int = 5,
    ) -> None:
        self._api_key = api_key or settings.gmgn_api_key
        headers: dict[str, str] = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        self._http = httpx.AsyncClient(timeout=30, headers=headers)
        self._semaphore = asyncio.Semaphore(requests_per_second)

    async def _request(
        self,
        path: str,
        *,
        params: dict | None = None,
        retries: int = 4,
    ) -> Any:
        url = f"{self.BASE_URL}{path}"
        backoff = 1.0

        for attempt in range(retries):
            resp = None
            transport_error: Exception | None = None

            async with self._semaphore:
                try:
                    resp = await self._http.get(url, params=params or {})
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
            data = resp.json()

            # GMGN uses code != 0 for application-level errors
            if isinstance(data, dict) and data.get("code", 0) != 0:
                raise httpx.HTTPStatusError(
                    f"GMGN error {data.get('code')}: {data.get('msg', 'unknown')}",
                    request=resp.request,
                    response=resp,
                )

            return data

        raise RuntimeError("GMGN request failed after all retries")

    async def get_wallet_profile(self, address: str) -> dict[str, Any]:
        """Fetch wallet stats: win rate, PnL, trade count, labels.

        Args:
            address: Solana wallet address.

        Returns:
            Raw GMGN response dict ({"code":0, "data":{...}}).
        """
        return await self._request(
            f"/smartmoney/sol/walletNew/{address}",
            params={"period": "30d"},
        )

    async def get_token_info(self, mint: str) -> dict[str, Any]:
        """Fetch token profile: market cap, holders, LP status, bundle %.

        Args:
            mint: SPL token mint address.

        Returns:
            Raw GMGN response dict.
        """
        return await self._request(f"/token/sol/{mint}")

    async def get_token_top_traders(
        self, mint: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Return the top traders for a token ranked by realised PnL.

        Args:
            mint: SPL token mint address.
            limit: Number of traders to return.

        Returns:
            List of trader dicts, each with address, profit, buy/sell amounts.
        """
        data = await self._request(
            f"/token/sol/top_traders/{mint}",
            params={"limit": limit, "orderby": "profit", "direction": "desc"},
        )
        # May be wrapped {"data": [...]} or a bare list
        if isinstance(data, dict):
            return data.get("data") or []
        return data or []

    async def close(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "GMGNClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()


# ── parsers ───────────────────────────────────────────────────────────────────

def parse_wallet_profile(address: str, raw: dict[str, Any]) -> Wallet:
    """Map a GMGN wallet profile response to a Wallet dataclass.

    smart_money_score is left at 0.0; call score_wallet() to populate it.

    Args:
        address: Wallet address (the API does not echo it in the body).
        raw: Full response dict from get_wallet_profile.

    Returns:
        Populated Wallet instance.
    """
    data: dict[str, Any] = raw.get("data") or raw

    win_rate = data.get("winrate")
    win_rate_f = float(win_rate) if win_rate is not None else None

    buys = int(data.get("buy") or 0)
    sells = int(data.get("sell") or 0)
    total_trades = buys + sells

    last_active = data.get("last_active_timestamp")
    first_seen = int(last_active) if last_active else None

    tags: list[str] = data.get("tags") or []
    label = tags[0] if tags else None

    return Wallet(
        address=address,
        label=label,
        smart_money_score=0.0,
        win_rate_90d=win_rate_f,
        total_trades=total_trades,
        first_seen=first_seen,
    )


def parse_token_info(mint: str, raw: dict[str, Any]) -> Token:
    """Map a GMGN token info response to a Token dataclass.

    Args:
        mint: Token mint address.
        raw: Full response dict from get_token_info.

    Returns:
        Populated Token instance.
    """
    data: dict[str, Any] = raw.get("data") or raw

    launchpad_raw = (data.get("launchpad") or data.get("platform") or "unknown").lower()
    if "pump" in launchpad_raw:
        launchpad = "pump.fun"
    elif "bag" in launchpad_raw:
        launchpad = "bags"
    else:
        launchpad = "unknown"

    burn_status = str(data.get("burn_status") or "").lower()
    lp_burned = burn_status in ("burned", "true") or bool(data.get("is_lp_burned"))

    top10_raw = data.get("top10_holder_rate")
    # GMGN may return a fraction (0.35) or a percentage (35.0); normalise to %
    if top10_raw is not None:
        top10_f = float(top10_raw)
        top10_pct: float | None = top10_f if top10_f > 1 else top10_f * 100
    else:
        top10_pct = None

    created_at = int(
        data.get("open_timestamp") or data.get("created_timestamp") or 0
    )

    return Token(
        mint=mint,
        symbol=data.get("symbol") or "",
        name=data.get("name") or "",
        launchpad=launchpad,
        created_at=created_at,
        market_cap_usd_snapshot=data.get("market_cap"),
        holders_count_snapshot=data.get("holder_count"),
        lp_burned=lp_burned,
        top10_pct=top10_pct,
    )
