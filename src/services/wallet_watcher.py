"""Background service — polls Helius for tracked wallet activity.

STATUS 2026-08-13: functional but producing nothing, and that is understood.
Helius's free tier is exhausted (CLAUDE.md records it as deprecated; a direct
probe refuses every call), so every sweep rate-limits and backs off. The service
is safe in that state — capped watchlist, capped concurrency, geometric backoff,
early abort — after a period in which it made 40.3 million consecutive refused
calls and wrote a 7.9 GB log.

Before porting it to the free RPC path, note that its output is largely REDUNDANT:
post_grad_swaps already flags smart-money wallets on every tracked coin (1,863
rows), because the tape captures every trade on a graduated coin regardless of who
made it. The unique value here is smart-money activity on coins we are NOT
tracking — i.e. pre-graduation. That is a product decision, not an ops repair, and
porting it means writing a raw-RPC swap parser (parse_swap expects Helius's
enhanced shape, not getTransaction's).
"""

import asyncio
import logging

from src.analyzer.smart_money import get_smart_money_wallets
from src.common.config import settings
from src.common.db import get_connection
from src.common.models import Wallet

logger = logging.getLogger(__name__)

POLL_INTERVAL = 60  # seconds between full wallet sweeps

# Concurrency cap. _tick used to asyncio.gather EVERY smart-money wallet at once,
# so each sweep launched N simultaneous requests and the provider answered all of
# them with 429. Measured on 2026-08-13: 40.3 million consecutive rate-limited
# calls, a 7.9 GB log, and zero useful alerts in the last 200k lines.
MAX_CONCURRENT = 4

# Watchlist cap. get_smart_money_wallets returns 17,891 wallets; polling all of
# them once a minute is ~298 requests/second, which no tier serves and which no
# amount of backoff makes reasonable. A watchlist is meant to be selective — take
# the highest-scoring wallets and poll those properly rather than all of them
# badly. At MAX_CONCURRENT a sweep of this size comfortably fits POLL_INTERVAL.
MAX_WATCHED = 200

# Abort a sweep once this many consecutive polls are refused. Without it the
# rate-limit check only ran AFTER a full sweep, so with a large watchlist the
# sweep never finished, backoff never engaged, and the service burned requests
# forever while looking idle.
ABORT_AFTER_CONSECUTIVE_LIMITS = 10

# Sustained rate-limiting means the budget is gone, not that we were unlucky.
# Backing off geometrically turns a hot loop into a quiet retry; without this the
# service cannot tell "temporarily throttled" from "this tier is exhausted" and
# burns CPU, network and disk on both.
BACKOFF_START_S = 60
BACKOFF_MAX_S = 3600
RATE_LIMIT_STREAK_TO_BACK_OFF = 5

# Per-wallet set of already-seen signatures so we only alert on genuinely new buys.
_seen_signatures: dict[str, set[str]] = {}


async def watch_wallets() -> None:
    """Main entry point — polls all smart money wallets every POLL_INTERVAL seconds."""
    from src.ingest.helius import HeliusClient

    logger.info("wallet watcher starting")
    backoff = 0
    async with HeliusClient() as helius:
        while True:
            try:
                polled, limited = await _tick(helius)
                if polled and limited == polled:
                    backoff = min(max(backoff * 2, BACKOFF_START_S), BACKOFF_MAX_S)
                    logger.warning(
                        "every wallet poll was rate-limited (%d/%d) — backing off %ds",
                        limited, polled, backoff)
                elif backoff:
                    logger.info("rate limiting cleared — resuming normal cadence")
                    backoff = 0
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("wallet watcher tick failed")
            await asyncio.sleep(backoff or POLL_INTERVAL)


async def _tick(helius: "object") -> tuple[int, int]:
    """Returns (wallets_polled, wallets_rate_limited)."""
    conn = get_connection()
    try:
        wallets = get_smart_money_wallets(conn)
    finally:
        conn.close()

    if not wallets:
        return 0, 0

    wallets = sorted(wallets, key=lambda w: -(w.smart_money_score or 0))[:MAX_WATCHED]

    sem = asyncio.Semaphore(MAX_CONCURRENT)
    state = {"streak": 0, "polled": 0, "limited": 0, "abort": False}

    async def guarded(w):
        if state["abort"]:
            return None
        async with sem:
            if state["abort"]:
                return None
            limited = await _poll_and_alert(w, helius)
        state["polled"] += 1
        if limited:
            state["limited"] += 1
            state["streak"] += 1
            if state["streak"] >= ABORT_AFTER_CONSECUTIVE_LIMITS:
                state["abort"] = True     # the tier is gone; stop asking
        else:
            state["streak"] = 0
        return limited

    await asyncio.gather(*(guarded(w) for w in wallets), return_exceptions=True)
    return state["polled"], state["limited"]


async def _poll_and_alert(wallet: Wallet, helius: "object") -> bool:
    """True if the poll was rate-limited."""
    try:
        new_buys = await poll_wallet(wallet, helius)
    except RateLimited:
        return True
    for buy in new_buys:
        await emit_alert(wallet, buy["token_mint"], buy["sol_amount"])
    return False


class RateLimited(Exception):
    """The provider refused the call — distinct from it returning nothing."""


def _is_rate_limit(exc: BaseException) -> bool:
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status in (429, 503):
        return True
    return "429" in str(exc) or "rate limit" in str(exc).lower()


async def poll_wallet(wallet: Wallet, helius: "object" = None) -> list[dict]:
    """Fetch and decode new token buys for a single wallet since last poll.

    Reuses the caller's client. It previously opened a NEW HeliusClient per wallet
    per sweep while ignoring the one it was passed, so a sweep of N wallets built
    and tore down N clients and shared no connection pool or rate-limit state.
    """
    from src.ingest.helius import HeliusClient, decode_swap_transaction

    try:
        if helius is not None:
            txs = await helius.get_transactions_for_address(wallet.address, limit=20)
        else:
            async with HeliusClient() as client:
                txs = await client.get_transactions_for_address(wallet.address, limit=20)
    except Exception as exc:
        # The client exhausts its own retries and then raises, so a 429 arrives
        # here as an HTTP error rather than an empty result. Distinguish it: a
        # rate limit means back off, any other failure is a real bug worth seeing.
        if _is_rate_limit(exc):
            raise RateLimited(wallet.address) from exc
        raise
    if txs is None:
        raise RateLimited(wallet.address)

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
    from src.common.logging_setup import quiet_url_loggers
    quiet_url_loggers()   # httpx logs API keys in URLs at INFO
    asyncio.run(watch_wallets())
