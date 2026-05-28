"""Pump.fun WebSocket listener via PumpPortal public API.

PumpPortal replaced the deprecated frontend-api.pump.fun Socket.IO endpoint.
It uses a standard WebSocket at wss://pumpportal.fun/api/data.

After connecting, subscribe by sending JSON method calls:
  {"method": "subscribeNewToken"}
  {"method": "subscribeTokenTrade", "keys": ["<mint>", ...]}

All events arrive as JSON with a "txType" field: "create", "buy", "sell".
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Coroutine

import aiohttp

logger = logging.getLogger(__name__)

PUMPPORTAL_WS = "wss://pumpportal.fun/api/data"

AsyncHandler = Callable[..., Coroutine[Any, Any, None]]


# ── data classes ──────────────────────────────────────────────────────────────

@dataclass
class NewCoin:
    """Token metadata broadcast at launch."""
    mint: str
    name: str
    symbol: str
    creator: str
    created_timestamp: int
    description: str = ""
    twitter: str | None = None
    telegram: str | None = None
    website: str | None = None
    image_uri: str | None = None
    market_cap_usd: float | None = None


@dataclass
class EarlyTrade:
    """A single buy or sell captured during the launch window."""
    mint: str
    user: str
    sol_amount: float      # SOL (PumpPortal sends SOL directly, not lamports)
    token_amount: float
    is_buy: bool
    timestamp: int


# ── client ────────────────────────────────────────────────────────────────────

class PumpListener:
    """Async WebSocket client that emits decoded NewCoin / EarlyTrade objects."""

    def __init__(self) -> None:
        self._coin_handlers: list[AsyncHandler] = []
        self._trade_handlers: list[AsyncHandler] = []
        self._connected_handlers: list[AsyncHandler] = []
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._pending_subs: list[str] = []

    def on_connected(self, fn: AsyncHandler) -> AsyncHandler:
        self._connected_handlers.append(fn)
        return fn

    def on_new_coin(self, fn: AsyncHandler) -> AsyncHandler:
        self._coin_handlers.append(fn)
        return fn

    def on_trade(self, fn: AsyncHandler) -> AsyncHandler:
        self._trade_handlers.append(fn)
        return fn

    async def subscribe_trades(self, mint: str) -> None:
        if self._ws and not self._ws.closed:
            await self._ws.send_json({"method": "subscribeTokenTrade", "keys": [mint]})
        else:
            self._pending_subs.append(mint)

    async def run(self) -> None:
        """Connect and run forever with auto-reconnect."""
        while True:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(
                        PUMPPORTAL_WS,
                        heartbeat=30,
                        timeout=aiohttp.ClientTimeout(total=None, connect=15),
                    ) as ws:
                        self._ws = ws
                        logger.info("connected to PumpPortal WebSocket")

                        await ws.send_json({"method": "subscribeNewToken"})

                        for mint in self._pending_subs:
                            await ws.send_json({"method": "subscribeTokenTrade", "keys": [mint]})
                        self._pending_subs.clear()

                        for h in self._connected_handlers:
                            asyncio.create_task(h())

                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                try:
                                    await self._dispatch(json.loads(msg.data))
                                except Exception:
                                    pass
                            elif msg.type in (
                                aiohttp.WSMsgType.CLOSED,
                                aiohttp.WSMsgType.ERROR,
                            ):
                                break

                        logger.warning("PumpPortal WebSocket closed — reconnecting")
            except Exception as exc:
                logger.warning("PumpPortal WS error: %s — retrying in 5s", exc)
            finally:
                self._ws = None
            await asyncio.sleep(5)

    async def _dispatch(self, data: dict) -> None:
        tx_type = data.get("txType") or data.get("type") or ""
        if tx_type == "create":
            coin = _parse_coin(data)
            if coin:
                for h in self._coin_handlers:
                    asyncio.create_task(h(coin))
        elif tx_type in ("buy", "sell"):
            trade = _parse_trade(data)
            if trade:
                for h in self._trade_handlers:
                    asyncio.create_task(h(trade))

    async def disconnect(self) -> None:
        if self._ws:
            await self._ws.close()

    async def __aenter__(self) -> "PumpListener":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.disconnect()


# ── parsers ───────────────────────────────────────────────────────────────────

def _parse_coin(raw: dict[str, Any]) -> NewCoin | None:
    mint = raw.get("mint")
    if not mint:
        return None

    mc_sol = raw.get("marketCapSol")
    # Rough USD estimate for display only — outcome_tracker uses real price snapshots
    mc_usd = float(mc_sol) * 150 if mc_sol else None

    meta = raw.get("metadata") or {}

    return NewCoin(
        mint=str(mint),
        name=str(raw.get("name") or meta.get("name") or ""),
        symbol=str(raw.get("symbol") or meta.get("symbol") or ""),
        description=str(raw.get("description") or meta.get("description") or ""),
        creator=str(raw.get("traderPublicKey") or raw.get("creator") or ""),
        created_timestamp=int(raw.get("timestamp") or raw.get("created_timestamp") or 0),
        twitter=raw.get("twitter") or meta.get("twitter") or None,
        telegram=raw.get("telegram") or meta.get("telegram") or None,
        website=raw.get("website") or meta.get("website") or None,
        image_uri=raw.get("imageUri") or raw.get("image_uri") or meta.get("imageUri") or None,
        market_cap_usd=mc_usd,
    )


def _parse_trade(raw: dict[str, Any]) -> EarlyTrade | None:
    mint = raw.get("mint")
    user = raw.get("traderPublicKey") or raw.get("user")
    if not mint or not user:
        return None

    return EarlyTrade(
        mint=str(mint),
        user=str(user),
        sol_amount=float(raw.get("solAmount") or 0),
        token_amount=float(raw.get("tokenAmount") or 0),
        is_buy=raw.get("txType") == "buy",
        timestamp=int(raw.get("timestamp") or 0),
    )
