"""Post-graduation distribution tracker.

After a Pump.fun token graduates to PumpSwap, early holders (team cluster +
BC snipers) are sitting in profit with thin liquidity (~$10-15K at migration).
This module tracks whether they distribute (sell into the market) or hold.

Checks at graduation_time + 1h / 4h / 24h from post_grad_behavior.
If the token is DUMPED at the 1h check the remaining checks still run
but will also find low liquidity and confirm DUMPED.

Signals:
  ACCUMULATING — net new buys by tracked wallets post-graduation (rare bullish)
  HOLDING      — minimal movement, wallets staying positioned
  DISTRIBUTING — selling accelerating, team reducing exposure
  DUMPED       — token effectively dead, liquidity gone or minimal
"""

import asyncio
import json
import logging
import time

from src.common.db import get_connection
from src.common.models import DistributionSignal, PostGradBehavior

logger = logging.getLogger(__name__)

CHECK_OFFSETS_H = (1, 4, 24)

# Classification thresholds
_DUMPED_HOLDER_THRESHOLD = 5       # fewer than 5 unique holders → DUMPED
_DISTRIBUTING_SELL_PCT   = 30.0   # team sold > 30% of grad-time position → DISTRIBUTING
_ACCUMULATING_BUY_PCT    = 10.0   # team grew position > 10% → ACCUMULATING


async def schedule_distribution_checks(
    token_mint: str, graduation_ts: int
) -> None:
    """Fire background tasks to check distribution at 1h, 4h, 24h post-graduation."""
    for offset_h in CHECK_OFFSETS_H:
        asyncio.create_task(
            _deferred_check(token_mint, graduation_ts, offset_h)
        )


async def _deferred_check(
    token_mint: str, graduation_ts: int, offset_h: int
) -> None:
    fire_at = graduation_ts + offset_h * 3600
    delay = fire_at - time.time()
    if delay > 0:
        await asyncio.sleep(delay)
    try:
        await _do_check(token_mint, offset_h)
    except Exception:
        logger.exception(
            "distribution check failed for %s at %dh", token_mint[:8], offset_h
        )


async def _do_check(token_mint: str, offset_h: int) -> PostGradBehavior | None:
    """Fetch current holder state, classify, persist, trigger downstream updates."""
    from src.ingest.helius import HeliusClient
    from src.common.cex_wallets import get_all_cex_addresses

    conn = get_connection()
    try:
        cex_addresses = get_all_cex_addresses(conn)

        cluster_row = conn.execute(
            """SELECT member_addresses, supply_pct_at_graduation
               FROM team_clusters WHERE token_mint = ?
               ORDER BY supply_pct_at_graduation DESC LIMIT 1""",
            (token_mint,),
        ).fetchone()
        team_addresses: set[str] = set()
        grad_team_pct: float = 0.0
        if cluster_row:
            team_addresses = set(json.loads(cluster_row["member_addresses"] or "[]"))
            grad_team_pct = float(cluster_row["supply_pct_at_graduation"] or 0)

        async with HeliusClient() as helius:
            accounts = await helius.get_token_largest_accounts(token_mint)

        if not accounts:
            return None

        # Filter out CEX wallets from the analysis
        accounts = [a for a in accounts if a.get("address") not in cex_addresses]

        total_supply = sum(float(a.get("uiAmount") or 0) for a in accounts)
        if total_supply == 0:
            return None

        current_team_pct = sum(
            float(a.get("uiAmount") or 0) / total_supply * 100
            for a in accounts
            if a.get("address") in team_addresses
        )

        team_sold_pct: float | None = None
        if grad_team_pct > 0:
            team_sold_pct = round(grad_team_pct - current_team_pct, 2)

        signal = _classify(
            team_sold_pct=team_sold_pct,
            holder_count=len(accounts),
        )

        behavior = PostGradBehavior(
            token_mint=token_mint,
            checked_at=int(time.time()),
            check_offset_h=offset_h,
            holders_remaining_count=len(accounts),
            team_sold_pct=team_sold_pct,
            snipers_sold_pct=None,   # TODO: track snipers separately
            liquidity_usd=None,      # TODO: fetch from PumpSwap pool via on-chain call
            distribution_signal=signal,
        )

        conn.execute(
            """INSERT INTO post_grad_behavior
               (token_mint, checked_at, check_offset_h, holders_remaining_count,
                team_sold_pct, snipers_sold_pct, liquidity_usd, distribution_signal)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(token_mint, check_offset_h) DO UPDATE SET
                   checked_at              = excluded.checked_at,
                   holders_remaining_count = excluded.holders_remaining_count,
                   team_sold_pct           = excluded.team_sold_pct,
                   distribution_signal     = excluded.distribution_signal""",
            (
                behavior.token_mint, behavior.checked_at, behavior.check_offset_h,
                behavior.holders_remaining_count, behavior.team_sold_pct,
                behavior.snipers_sold_pct, behavior.liquidity_usd,
                behavior.distribution_signal.value,
            ),
        )
        conn.commit()

        logger.info(
            "distribution %dh — %s  team_sold=%.1f%%  signal=%s",
            offset_h, token_mint[:8],
            team_sold_pct or 0.0,
            signal.value,
        )

        # Sync to Supabase (fire-and-forget)
        import asyncio
        from src.common import supabase_sync as sb
        asyncio.create_task(sb.post_grad_behavior(
            token_mint=token_mint,
            check_offset_h=offset_h,
            checked_at=behavior.checked_at,
            holders_remaining_count=behavior.holders_remaining_count,
            team_sold_pct=behavior.team_sold_pct,
            distribution_signal=signal.value,
        ))

        # Record first DISTRIBUTING signal for dump timing memory
        if signal == DistributionSignal.DISTRIBUTING:
            _record_first_dump(token_mint, offset_h, conn)

        # At 4h: update funder reputation + fingerprint + wallet graph with outcome
        if offset_h == 4:
            await _update_funder_reputation_from_distribution(token_mint, conn)

        return behavior
    finally:
        conn.close()


def _record_first_dump(token_mint: str, offset_h: int, conn) -> None:
    """Record dump timing for the funder if this is the first DISTRIBUTING signal."""
    from src.analyzer.team_memory import record_dump_start

    # Only record once per token (check previous checks at lower offsets)
    if offset_h > 1:
        prev = conn.execute(
            """SELECT 1 FROM post_grad_behavior
               WHERE token_mint = ? AND check_offset_h < ?
                 AND distribution_signal = 'DISTRIBUTING' LIMIT 1""",
            (token_mint, offset_h),
        ).fetchone()
        if prev:
            return  # already recorded at an earlier check

    funder_row = conn.execute(
        """SELECT tc.funding_source FROM team_clusters tc
           WHERE tc.token_mint = ? AND tc.funding_source IS NOT NULL
             AND tc.funding_source != 'cex' LIMIT 1""",
        (token_mint,),
    ).fetchone()
    if funder_row:
        record_dump_start(funder_row["funding_source"], offset_h, conn)


async def _update_funder_reputation_from_distribution(
    token_mint: str, conn
) -> None:
    """After 4h distribution check, update funder_reputation + fingerprint + wallet graph."""
    from src.analyzer.smart_money import update_funder_reputation

    outcome_row = conn.execute(
        "SELECT classified FROM coin_outcomes WHERE token_mint = ? AND check_offset_h = 4",
        (token_mint,),
    ).fetchone()
    if not outcome_row or not outcome_row["classified"]:
        return

    funder_row = conn.execute(
        """SELECT w.funding_source
           FROM token_buyers tb
           JOIN wallets w ON w.address = tb.wallet_address
           WHERE tb.token_mint = ?
             AND w.funding_source IS NOT NULL
             AND w.funding_source != 'cex'
           GROUP BY w.funding_source ORDER BY COUNT(*) DESC LIMIT 1""",
        (token_mint,),
    ).fetchone()
    if not funder_row:
        return

    token_row = conn.execute(
        "SELECT bundle_pct, dev_pct FROM tokens WHERE mint = ?", (token_mint,)
    ).fetchone()
    bundle_pct = float(token_row["bundle_pct"] or 0) if token_row else 0.0
    dev_pct = float(token_row["dev_pct"] or 0) if token_row else 0.0

    update_funder_reputation(
        funder_row["funding_source"],
        token_mint,
        outcome_row["classified"],
        bundle_pct,
        dev_pct,
        conn,
    )

    # Memory: update wallet graph with outcome + update structural fingerprint
    from src.analyzer.team_memory import update_wallet_graph, update_fingerprint
    from src.common.models import TeamCluster

    cluster_row = conn.execute(
        """SELECT cluster_id, member_addresses, supply_pct_at_graduation,
                  first_buy_offset_seconds, is_bc_sniper
           FROM team_clusters WHERE token_mint = ? LIMIT 1""",
        (token_mint,),
    ).fetchone()

    if cluster_row:
        members = json.loads(cluster_row["member_addresses"] or "[]")
        update_wallet_graph(members, outcome=outcome_row["classified"], conn=conn)

        tc = TeamCluster(
            cluster_id=cluster_row["cluster_id"],
            token_mint=token_mint,
            funding_source=funder_row["funding_source"],
            member_addresses=members,
            supply_pct_at_graduation=float(cluster_row["supply_pct_at_graduation"] or 0),
            first_buy_offset_seconds=float(cluster_row["first_buy_offset_seconds"] or 0),
            is_bc_sniper=bool(cluster_row["is_bc_sniper"]),
        )
        update_fingerprint(tc, outcome=outcome_row["classified"], conn=conn)


def _classify(
    team_sold_pct: float | None,
    holder_count: int,
) -> DistributionSignal:
    if holder_count < _DUMPED_HOLDER_THRESHOLD:
        return DistributionSignal.DUMPED
    if team_sold_pct is None:
        return DistributionSignal.HOLDING
    if team_sold_pct >= _DISTRIBUTING_SELL_PCT:
        return DistributionSignal.DISTRIBUTING
    if team_sold_pct <= -_ACCUMULATING_BUY_PCT:
        return DistributionSignal.ACCUMULATING
    return DistributionSignal.HOLDING


def get_latest_signal(token_mint: str, conn) -> DistributionSignal | None:
    """Return the most recent distribution signal for a token, or None."""
    row = conn.execute(
        """SELECT distribution_signal FROM post_grad_behavior
           WHERE token_mint = ?
           ORDER BY check_offset_h DESC LIMIT 1""",
        (token_mint,),
    ).fetchone()
    if not row:
        return None
    try:
        return DistributionSignal(row["distribution_signal"])
    except (ValueError, KeyError):
        return None
