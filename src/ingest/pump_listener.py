"""Pump.fun WebSocket listener — real-time new-token and trade events.

Pump.fun uses Socket.IO 4.x at https://frontend-api.pump.fun.

Key events (as of mid-2024 — verify against live traffic if behaviour changes):
  newCoinCreated  — fired immediately when a new token is deployed
  tradeCreated    — fired on every buy/sell

To subscribe to trades for a specific mint, emit "joinCoinRoom" with
{"mint": "<address>"} after connecting.

sol_amount in tradeCreated events comes in as raw lamports (÷ 1e9 → SOL).
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Callable, Coroutine

import socketio

logger = logging.getLogger(__name__)

PUMP_FUN_API = "https://frontend-api.pump.fun"

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
    sol_amount: float      # SOL (already converted from lamports)
    token_amount: float
    is_buy: bool
    timestamp: int


# ── client ────────────────────────────────────────────────────────────────────

class PumpListener:
    """Async Socket.IO client that emits decoded NewCoin / EarlyTrade objects."""

    def __init__(self) -> None:
        self._sio = socketio.AsyncClient(logger=False, engineio_logger=False)
        self._coin_handlers: list[AsyncHandler] = []
        self._trade_handlers: list[AsyncHandler] = []
        self._connected_handlers: list[AsyncHandler] = []
        self._setup_handlers()

    def _setup_handlers(self) -> None:
        sio = self._sio

        @sio.on("connect")
        async def _on_connect() -> None:
            logger.info("connected to Pump.fun WebSocket")
            for h in self._connected_handlers:
                asyncio.create_task(h())

        @sio.on("disconnect")
        async def _on_disconnect() -> None:
            logger.warning("disconnected from Pump.fun WebSocket")

        @sio.on("newCoinCreated")
        async def _on_new_coin(data: dict[str, Any]) -> None:
            coin = _parse_coin(data)
            if coin:
                for h in self._coin_handlers:
                    asyncio.create_task(h(coin))

        @sio.on("tradeCreated")
        async def _on_trade(data: dict[str, Any]) -> None:
            trade = _parse_trade(data)
            if trade:
                for h in self._trade_handlers:
                    asyncio.create_task(h(trade))

    # ── handler registration ──────────────────────────────────────────────────

    def on_connected(self, fn: AsyncHandler) -> AsyncHandler:
        self._connected_handlers.append(fn)
        return fn

    def on_new_coin(self, fn: AsyncHandler) -> AsyncHandler:
        self._coin_handlers.append(fn)
        return fn

    def on_trade(self, fn: AsyncHandler) -> AsyncHandler:
        self._trade_handlers.append(fn)
        return fn

    # ── transport ─────────────────────────────────────────────────────────────

    async def subscribe_trades(self, mint: str) -> None:
        """Join the per-mint trade room so tradeCreated events arrive for this mint."""
        await self._sio.emit("joinCoinRoom", {"mint": mint})

    async def run(self) -> None:
        """Connect and block until the socket disconnects."""
        await self._sio.connect(PUMP_FUN_API, transports=["websocket"])
        await self._sio.wait()

    async def disconnect(self) -> None:
        await self._sio.disconnect()

    async def __aenter__(self) -> "PumpListener":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.disconnect()


# ── parsers ───────────────────────────────────────────────────────────────────

def _parse_coin(raw: dict[str, Any]) -> NewCoin | None:
    mint = raw.get("mint")
    if not mint:
        return None
    mc = raw.get("usd_market_cap") or raw.get("market_cap")
    return NewCoin(
        mint=str(mint),
        name=str(raw.get("name") or ""),
        symbol=str(raw.get("symbol") or ""),
        description=str(raw.get("description") or ""),
        creator=str(raw.get("creator") or raw.get("creator_pubkey") or ""),
        created_timestamp=int(raw.get("created_timestamp") or 0),
        twitter=raw.get("twitter") or None,
        telegram=raw.get("telegram") or None,
        website=raw.get("website") or None,
        image_uri=raw.get("image_uri") or None,
        market_cap_usd=float(mc) if mc else None,
    )


def _parse_trade(raw: dict[str, Any]) -> EarlyTrade | None:
    mint = raw.get("mint")
    user = raw.get("user") or raw.get("trader_public_key")
    if not mint or not user:
        return None

    # sol_amount arrives as lamports in the WebSocket feed
    lamports = float(raw.get("sol_amount") or 0)
    sol = lamports / 1_000_000_000

    return EarlyTrade(
        mint=str(mint),
        user=str(user),
        sol_amount=sol,
        token_amount=float(raw.get("token_amount") or 0),
        is_buy=bool(raw.get("is_buy", True)),
        timestamp=int(raw.get("timestamp") or 0),
    )
