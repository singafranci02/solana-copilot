"""Real-time Pump.fun launch monitor.

For every new coin that appears on Pump.fun:

  t = 0 s   newCoinCreated arrives
            → immediate narrative tags extracted from name/symbol/description
            → subscribe to the coin's trade room
            → check if creator is already a known smart money / dev wallet

  t = 60 s  collection window closes
            → cluster early buyers by funding source (Helius)
            → score smart money overlap
            → run LLM summary
            → emit a formatted alert to stdout + persist to DB

Tokens with fewer than MIN_BUYERS_TO_ANALYSE unique buyers in the first
60 s are skipped (test tokens / no activity).
"""

import asyncio
import json
import logging
import time
from collections import defaultdict

from src.analyzer.narrative_match import get_active_narratives, match_token_to_narratives
from src.analyzer.smart_money import find_smart_money_in_buyers, get_smart_money_wallets
from src.analyzer.summarize import SmartMoneyEntry, TokenAnalysis, summarize
from src.analyzer.team_detect import compute_dev_pct, identify_team_cluster
from src.analyzer.wallet_cluster import build_clusters, compute_bundle_pct
from src.common.config import settings
from src.common.db import get_connection
from src.common.models import Token, TokenBuyer
from src.ingest.pump_listener import EarlyTrade, NewCoin, PumpListener

logger = logging.getLogger(__name__)

COLLECTION_WINDOW = settings.collection_window_seconds
MIN_BUYERS = settings.min_buyers_to_analyse


# ── main entry point ──────────────────────────────────────────────────────────

async def monitor() -> None:
    """Connect to Pump.fun WebSocket and analyse every new launch."""
    listener = PumpListener()
    pending: dict[str, list[EarlyTrade]] = defaultdict(list)

    @listener.on_new_coin
    async def on_new_coin(coin: NewCoin) -> None:
        logger.info("launch detected: $%s  %s", coin.symbol, coin.mint[:8])
        await listener.subscribe_trades(coin.mint)
        asyncio.create_task(_collect_then_analyse(coin, pending))

    @listener.on_trade
    async def on_trade(trade: EarlyTrade) -> None:
        if trade.is_buy:
            pending[trade.mint].append(trade)

    logger.info("pump monitor running — listening for new launches")
    await listener.run()   # blocks until disconnect


# ── pipeline ──────────────────────────────────────────────────────────────────

async def _collect_then_analyse(
    coin: NewCoin, pending: dict[str, list[EarlyTrade]]
) -> None:
    await asyncio.sleep(COLLECTION_WINDOW)

    trades = pending.pop(coin.mint, [])
    unique_buyers = {t.user for t in trades}

    if len(unique_buyers) < MIN_BUYERS:
        logger.debug(
            "skipping $%s — only %d unique buyer(s) in first %ds",
            coin.symbol, len(unique_buyers), COLLECTION_WINDOW,
        )
        return

    try:
        await analyse_launch(coin, trades)
    except Exception:
        logger.exception("analysis failed for $%s (%s)", coin.symbol, coin.mint)


async def analyse_launch(coin: NewCoin, trades: list[EarlyTrade]) -> None:
    """Full analysis pipeline for a launched coin and its collected early trades."""
    from src.ingest.helius import HeliusClient

    token = Token(
        mint=coin.mint,
        symbol=coin.symbol,
        name=coin.name,
        launchpad="pump.fun",
        created_at=coin.created_timestamp or int(time.time()),
        market_cap_usd_snapshot=coin.market_cap_usd,
        narrative_tags=extract_narrative_tags(coin),
    )

    buyers = [
        TokenBuyer(
            token_mint=t.mint,
            wallet_address=t.user,
            bought_at=t.timestamp or int(time.time()),
            sol_amount=t.sol_amount,
            tokens_received=t.token_amount,
        )
        for t in trades
    ]

    conn = get_connection()
    try:
        async with HeliusClient() as helius:
            clusters = await build_clusters(buyers, token.created_at, helius)

        bundle_pct = compute_bundle_pct(clusters, buyers)
        token.bundle_pct = bundle_pct

        smart_money_list = get_smart_money_wallets(conn)
        sm_buyers = find_smart_money_in_buyers(buyers, smart_money_list)

        team_cluster = identify_team_cluster(
            token, clusters, smart_money_list, deployer=coin.creator
        )
        dev_pct = compute_dev_pct(team_cluster, buyers) if team_cluster else 0.0
        token.dev_pct = dev_pct

        # Check if this funder is a known serial rugger from our own history
        from src.analyzer.outcome_tracker import get_team_fingerprint
        known_rugger = None
        if team_cluster and team_cluster.funding_source not in ("cex", None):
            fp = get_team_fingerprint(team_cluster.funding_source, conn)
            if fp and fp["rug_rate"] > 0.5 and len(fp["known_mints"]) >= 2:
                known_rugger = fp

        active_narratives = get_active_narratives(conn)
        matched_narratives = match_token_to_narratives(token, active_narratives)

        sm_entries = [SmartMoneyEntry(wallet=w) for w in sm_buyers]

        analysis = TokenAnalysis(
            token=token,
            team_cluster=team_cluster,
            smart_money_entries=sm_entries,
            matched_narratives=matched_narratives,
            narrative_states=active_narratives,
            past_deployments=[],
            raw_stats={"bundle_pct": bundle_pct, "dev_pct": dev_pct},
            token_launch_ts=token.created_at,
            social_handle=_twitter_handle(coin.twitter),
        )

        result = await summarize(analysis, provider=settings.llm_provider)

        # Persist token to DB
        conn.execute(
            """INSERT OR REPLACE INTO tokens
               (mint, symbol, name, launchpad, created_at, market_cap_usd_snapshot,
                lp_burned, bundle_pct, dev_pct, narrative_tags)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                token.mint, token.symbol, token.name, token.launchpad, token.created_at,
                token.market_cap_usd_snapshot, 0,
                bundle_pct, dev_pct, json.dumps(matched_narratives),
            ),
        )
        conn.commit()

        _print_alert(coin, result, sm_buyers, bundle_pct, dev_pct, matched_narratives,
                     known_rugger=known_rugger)

        # Schedule outcome checks at 1h / 4h / 24h — this is how the system learns
        from src.analyzer.outcome_tracker import schedule_checks
        await schedule_checks(coin.mint, token.market_cap_usd_snapshot)

    finally:
        conn.close()


# ── helpers ───────────────────────────────────────────────────────────────────

# Ordered most-to-least specific so a token called "AITRUMP" picks up both.
_NARRATIVE_KEYWORDS: list[tuple[str, str]] = [
    # meme archetypes
    ("pepe", "pepe"), ("frog", "pepe"),
    ("doge", "doge"), ("shib", "doge"), ("dog", "doge"),
    ("cat", "cat"), ("kitty", "cat"), ("nyan", "cat"),
    ("wojak", "wojak"), ("chad", "wojak"), ("bobo", "wojak"),
    # political / cultural
    ("trump", "trump"), ("maga", "trump"), ("potus", "trump"),
    ("elon", "elon"), ("musk", "elon"), ("tesla", "elon"),
    ("biden", "biden"),
    # tech meta
    ("ai", "ai"), ("agent", "ai"), ("gpt", "ai"), ("neural", "ai"),
    ("base", "base"), ("based", "base"),
    # chain meta
    ("solana", "solana"), ("sol", "solana"),
    ("bitcoin", "bitcoin"), ("btc", "bitcoin"),
    ("eth", "eth"), ("ethereum", "eth"),
]


def extract_narrative_tags(coin: NewCoin) -> list[str]:
    """Extract narrative keywords from name, symbol, and description."""
    text = f"{coin.name} {coin.symbol} {coin.description}".lower()
    seen: set[str] = set()
    tags: list[str] = []
    for keyword, tag in _NARRATIVE_KEYWORDS:
        if keyword in text and tag not in seen:
            tags.append(tag)
            seen.add(tag)
    return tags


def _twitter_handle(url: str | None) -> str | None:
    if not url:
        return None
    for prefix in ("https://x.com/", "https://twitter.com/",
                   "http://x.com/", "http://twitter.com/"):
        if url.startswith(prefix):
            handle = url[len(prefix):].split("/")[0].split("?")[0]
            return handle or None
    return None


def _print_alert(
    coin: NewCoin,
    result: "object",
    sm_buyers: list,
    bundle_pct: float,
    dev_pct: float,
    matched_narratives: list[str],
    known_rugger: dict | None = None,
) -> None:
    meta = result.metadata  # type: ignore[attr-defined]
    text = result.text      # type: ignore[attr-defined]

    signals = []
    if sm_buyers:
        signals.append(f"{len(sm_buyers)} smart money")
    if bundle_pct and bundle_pct > 15:
        signals.append(f"bundle {bundle_pct:.0f}%")
    if dev_pct and dev_pct > 10:
        signals.append(f"dev {dev_pct:.0f}%")
    if matched_narratives:
        signals.append(", ".join(matched_narratives))
    signals_str = " | ".join(signals) if signals else "no strong signals"

    rugger_line = ""
    if known_rugger:
        rugger_line = (
            f"\n  ⚠ KNOWN RUGGER — {len(known_rugger['known_mints'])} prev launches, "
            f"rug rate {known_rugger['rug_rate']*100:.0f}%"
        )

    logger.info(
        "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "  $%s — %s\n"
        "  %s%s\n"
        "  Signals: %s\n"
        "  Confidence: %s  |  Suggested: %.1f%% position\n"
        "  %s\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        coin.symbol, coin.name, coin.mint, rugger_line,
        signals_str,
        meta.confidence, meta.suggested_position_pct,
        text,
    )


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    asyncio.run(monitor())
