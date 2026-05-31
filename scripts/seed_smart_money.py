"""Seed smart money wallets from graduated token BC buyers + GMGN enrichment.

Strategy:
  1. Collect wallet addresses from BC buyers of our graduated tokens (own data)
  2. Supplement with GMGN top-traders for those same tokens
  3. Optionally add explicit addresses from CLI args
  4. Enrich each address via GMGN wallet profile (win rate, trade count, labels)
  5. Score using external formula (no recency penalty — they're new to our DB)
  6. Upsert into wallets table if score >= threshold

Run on Mac mini:
    uv run python scripts/seed_smart_money.py
    uv run python scripts/seed_smart_money.py <addr1> <addr2> ...  (add extra addresses)
"""

import asyncio
import logging
import sys
import time
from pathlib import Path

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.common.db import get_connection
from src.common.cex_wallets import is_cex_wallet
from src.analyzer.smart_money import upsert_wallet
from src.ingest.gmgn import GMGNClient, parse_wallet_profile
from src.common.models import Wallet

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── thresholds ────────────────────────────────────────────────────────────────

MIN_WIN_RATE   = 0.55   # GMGN 30d win rate minimum
MIN_TRADES     = 30     # minimum trade count for signal reliability
SEED_THRESHOLD = 0.60   # external scoring threshold (lower than in-system 0.7)
MAX_WALLETS    = 200    # cap to avoid hammering GMGN


def collect_candidate_addresses(conn) -> set[str]:
    """Gather wallet addresses from BC buyers of our graduated tokens."""
    # BC buyers of graduated tokens — wallets that bought tokens that graduated
    rows = conn.execute(
        """SELECT DISTINCT tb.wallet_address
           FROM token_buyers tb
           JOIN graduation_events ge ON ge.token_mint = tb.token_mint
           WHERE ge.structural_verdict IS NOT NULL
           LIMIT ?""",
        (MAX_WALLETS * 2,),
    ).fetchall()

    addresses = {r["wallet_address"] for r in rows if r["wallet_address"]}
    logger.info("collected %d BC buyer addresses from graduated tokens", len(addresses))
    return addresses


async def enrich_and_seed(addresses: set[str], conn) -> int:
    """Fetch GMGN profiles, score, and seed wallets that meet the threshold."""
    seeded = 0
    skipped_cex = 0
    skipped_score = 0
    errors = 0

    # api_key="" — GMGN public endpoints work without auth
    async with GMGNClient(api_key="") as gmgn:
        for i, address in enumerate(sorted(addresses)):

            # Skip known CEX wallets
            if is_cex_wallet(address, conn):
                skipped_cex += 1
                continue

            # Already seeded and scored well — skip re-enrichment
            existing = conn.execute(
                "SELECT smart_money_score FROM wallets WHERE address = ?",
                (address,),
            ).fetchone()
            if existing and float(existing["smart_money_score"] or 0) >= 0.70:
                continue

            try:
                raw = await gmgn.get_wallet_profile(address)
                wallet = parse_wallet_profile(address, raw)

                wr = float(wallet.win_rate_90d or 0)
                trades = int(wallet.total_trades or 0)

                if wr < MIN_WIN_RATE or trades < MIN_TRADES:
                    skipped_score += 1
                    continue

                # External scoring: no recency since wallet isn't in token_buyers yet
                # Use higher win_rate weight to compensate
                volume_signal = min(trades / 500.0, 1.0)
                score = round(0.70 * wr + 0.30 * volume_signal, 4)

                # Boost GMGN-labelled smart money wallets
                label = wallet.label or ""
                if "smart" in label.lower() or "kol" in label.lower():
                    score = max(score, 0.72)

                wallet.smart_money_score = score

                if score >= SEED_THRESHOLD:
                    upsert_wallet(wallet, conn)
                    seeded += 1
                    logger.info(
                        "SEEDED %s..  score=%.2f  win_rate=%.0f%%  trades=%d  label=%s",
                        address[:8], score, wr * 100, trades, label or "—",
                    )
                else:
                    skipped_score += 1

                # Gentle rate limit — GMGN allows ~5 req/s but be conservative
                await asyncio.sleep(0.25)

            except KeyboardInterrupt:
                break
            except Exception as exc:
                errors += 1
                logger.debug("SKIP %s..  error: %s", address[:8], exc)

            if i > 0 and i % 20 == 0:
                logger.info("progress: %d/%d  seeded=%d", i, len(addresses), seeded)

    logger.info(
        "done — seeded=%d  skipped_score=%d  skipped_cex=%d  errors=%d",
        seeded, skipped_score, skipped_cex, errors,
    )
    return seeded


async def main() -> None:
    conn = get_connection()

    # Base set: BC buyers of our graduated tokens
    addresses = collect_candidate_addresses(conn)

    # Supplement with any manually supplied addresses from CLI
    for arg in sys.argv[1:]:
        addr = arg.strip()
        if len(addr) >= 32:
            addresses.add(addr)
            logger.info("added manual address: %s..", addr[:8])

    if not addresses:
        logger.warning(
            "no addresses to enrich — run the graduation monitor for a while first, "
            "or pass known wallet addresses as CLI arguments"
        )
        conn.close()
        return

    # Cap total to avoid long runs
    if len(addresses) > MAX_WALLETS:
        logger.info("capping to %d addresses (from %d)", MAX_WALLETS, len(addresses))
        addresses = set(list(addresses)[:MAX_WALLETS])

    seeded = await enrich_and_seed(addresses, conn)

    # Report current state
    total_sm = conn.execute(
        "SELECT COUNT(*) FROM wallets WHERE smart_money_score >= 0.7"
    ).fetchone()[0]
    logger.info("smart money wallets in DB now: %d", total_sm)

    conn.close()


if __name__ == "__main__":
    asyncio.run(main())
