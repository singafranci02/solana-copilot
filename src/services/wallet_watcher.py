"""Background service — polls Helius for tracked wallet activity."""

import asyncio
import logging

from src.analyzer.smart_money import get_smart_money_wallets
from src.common.config import settings
from src.common.db import get_connection
from src.common.models import Wallet

logger = logging.getLogger(__name__)

POLL_INTERVAL = 60  # seconds between full wallet sweeps

# Per-wallet set of already-seen signatures so we only alert on genuinely new buys.
_seen_signatures: dict[str, set[str]] = {}


async def watch_wallets() -> None:
    """Main entry point — polls all smart money wallets every POLL_INTERVAL seconds."""
    from src.ingest.helius import HeliusClient

    logger.info("wallet watcher starting")
    async with HeliusClient() as helius:
        while True:
            try:
                await _tick(helius)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("wallet watcher tick failed")
            await asyncio.sleep(POLL_INTERVAL)


async def _tick(helius: "object") -> None:
    conn = get_connection()
    try:
        wallets = get_smart_money_wallets(conn)
    finally:
        conn.close()

    if not wallets:
        return

    tasks = [_poll_and_alert(w, helius) for w in wallets]
    await asyncio.gather(*tasks, return_exceptions=True)


async def _poll_and_alert(wallet: Wallet, helius: "object") -> None:
    new_buys = await poll_wallet(wallet)
    for buy in new_buys:
        await emit_alert(wallet, buy["token_mint"], buy["sol_amount"])


async def poll_wallet(wallet: Wallet) -> list[dict]:
    """Fetch and decode new token buys for a single wallet since last poll."""
    from src.ingest.helius import HeliusClient, decode_swap_transaction

    async with HeliusClient() as helius:
        txs = await helius.get_transactions_for_address(wallet.address, limit=20)

    seen = _seen_signatures.setdefault(wallet.address, set())
    new_buys: list[dict] = []

    for tx in txs:
        sig = tx.get("signature", "")
        if sig in seen:
            continue
        seen.add(sig)

        buyer = decode_swap_transaction(tx)
        if buyer and buyer.wallet_address == wallet.address:
            new_buys.append({
                "token_mint": buyer.token_mint,
                "sol_amount": buyer.sol_amount,
                "bought_at": buyer.bought_at,
                "signature": sig,
            })

    return new_buys


async def emit_alert(wallet: Wallet, token_mint: str, sol_amount: float) -> None:
    """Log a smart money buy alert."""
    logger.info(
        "SMART MONEY ALERT | wallet=...%s | token=...%s | sol=%.3f | score=%.2f",
        wallet.address[-6:],
        token_mint[-6:],
        sol_amount,
        wallet.smart_money_score,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(watch_wallets())
