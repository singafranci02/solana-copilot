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

_ALIVE_LIQUIDITY_FLOOR   = 500.0  # below this USD liquidity, skip per-wallet tx fetch


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
    from src.ingest.solana_tracker import SolanaTrackerClient
    from src.common.cex_wallets import get_all_cex_addresses

    conn = get_connection()
    try:
        cex_addresses = get_all_cex_addresses(conn)

        cluster_row = conn.execute(
            """SELECT member_addresses, supply_pct_at_graduation, is_bc_sniper
               FROM team_clusters WHERE token_mint = ?
               ORDER BY supply_pct_at_graduation DESC LIMIT 1""",
            (token_mint,),
        ).fetchone()
        team_addresses: set[str] = set()
        grad_team_pct: float = 0.0
        is_bc_sniper = False
        if cluster_row:
            team_addresses = set(json.loads(cluster_row["member_addresses"] or "[]"))
            grad_team_pct = float(cluster_row["supply_pct_at_graduation"] or 0)
            is_bc_sniper = bool(cluster_row["is_bc_sniper"])

        async with SolanaTrackerClient() as st:
            accounts = await st.get_token_holders(token_mint)

        if not accounts:
            return None

        # Exclude CEX + structural accounts (AMM pool, curve, programs). The
        # PumpSwap pool is the largest "holder" post-migration — leaving it in
        # corrupts total supply, team pct, and the tracked top-20 set.
        from src.analyzer.structural_accounts import (
            structural_set, PUMP_FUN_TOTAL_SUPPLY,
        )
        excluded = structural_set(
            None, cex_addresses, extra=_load_pool_accounts(token_mint, conn)
        )
        accounts = [a for a in accounts if a.get("address") not in excluded]

        supply_row = conn.execute(
            "SELECT total_supply FROM tokens WHERE mint = ?", (token_mint,)
        ).fetchone()
        total_supply = (
            float(supply_row["total_supply"])
            if supply_row and supply_row["total_supply"]
            else PUMP_FUN_TOTAL_SUPPLY
        )

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

        # ── Transaction-level behaviour + holder trajectory ───────────────────
        # Fetch DexScreener once: liquidity (dead-token guard) + USD price (F4b).
        liquidity_usd, price_usd, _pair = await _fetch_dex_stats(token_mint)

        metrics = None
        team_swaps = []
        holder_metrics = None
        new_sm_count = 0
        if liquidity_usd is None or liquidity_usd >= _ALIVE_LIQUIDITY_FLOOR:
            from src.analyzer.post_grad_swaps import (
                fetch_team_swaps, compute_metrics, upsert_swaps,
            )
            from src.analyzer.holder_snapshot import compute_holder_snapshot, detect_new_entrants
            from src.analyzer.smart_money import get_smart_money_wallets

            grad_positions = _load_grad_positions(token_mint, conn)
            grad_holder_set = set(grad_positions.keys())
            graduated_at = _load_graduated_at(token_mint, conn)

            # F2: track team ∪ top-20 current holders (not just the team cluster)
            top_holders = {
                a.get("address") for a in accounts
                if a.get("address") and a.get("address") not in excluded
            }
            tracked = sorted(team_addresses | top_holders)
            smart_money_set = {w.address for w in get_smart_money_wallets(conn)}

            if tracked:
                async with SolanaTrackerClient() as st2:
                    team_swaps = await fetch_team_swaps(
                        st2, token_mint, tracked, since_ts=graduated_at,
                    )
                sniper_set = team_addresses if is_bc_sniper else set()
                upsert_swaps(
                    conn, token_mint, team_swaps, sniper_set,
                    team_wallets=team_addresses, smart_money_wallets=smart_money_set,
                )
                # team-only metrics for the behavior row (keeps signal meaning stable)
                team_only_swaps = [s for s in team_swaps if s.signer in team_addresses]
                metrics = compute_metrics(
                    team_only_swaps, grad_positions, sniper_set or team_addresses
                )
                # F2: new smart-money entrants post-graduation
                swap_wallets = {s.signer for s in team_swaps if s.side == "buy"}
                entrants = detect_new_entrants(swap_wallets, grad_holder_set, smart_money_set)
                new_sm_count = sum(1 for e in entrants if e.is_smart_money)

            # F3: holder trajectory snapshot
            holder_metrics = compute_holder_snapshot(accounts, grad_holder_set, total_supply)
            top10_value_usd = (
                round(holder_metrics.top10_pct / 100 * total_supply * price_usd, 2)
                if price_usd else None
            )
            conn.execute(
                """INSERT INTO holder_snapshots
                       (token_mint, checked_at, check_offset_h, holder_count,
                        holder_count_is_total, top10_pct, new_holder_count,
                        churned_holder_count, new_smart_money_count, top10_value_usd)
                   VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
                   ON CONFLICT(token_mint, check_offset_h) DO UPDATE SET
                       checked_at            = excluded.checked_at,
                       holder_count          = excluded.holder_count,
                       top10_pct             = excluded.top10_pct,
                       new_holder_count      = excluded.new_holder_count,
                       churned_holder_count  = excluded.churned_holder_count,
                       new_smart_money_count = excluded.new_smart_money_count,
                       top10_value_usd       = excluded.top10_value_usd""",
                (
                    token_mint, int(time.time()), offset_h, holder_metrics.holder_count,
                    holder_metrics.top10_pct, holder_metrics.new_holder_count,
                    holder_metrics.churned_holder_count, new_sm_count, top10_value_usd,
                ),
            )
            conn.commit()
            from src.common import supabase_sync as _sb
            asyncio.create_task(_sb.holder_snapshot(
                token_mint=token_mint, check_offset_h=offset_h, checked_at=int(time.time()),
                holder_count=holder_metrics.holder_count, holder_count_is_total=False,
                top10_pct=holder_metrics.top10_pct,
                new_holder_count=holder_metrics.new_holder_count,
                churned_holder_count=holder_metrics.churned_holder_count,
                new_smart_money_count=new_sm_count, top10_value_usd=top10_value_usd,
            ))

        behavior = PostGradBehavior(
            token_mint=token_mint,
            checked_at=int(time.time()),
            check_offset_h=offset_h,
            holders_remaining_count=len(accounts),
            team_sold_pct=team_sold_pct,
            snipers_sold_pct=metrics.snipers_sold_pct if metrics else None,
            liquidity_usd=liquidity_usd,
            team_buy_count=metrics.team_buy_count if metrics else 0,
            team_sell_count=metrics.team_sell_count if metrics else 0,
            team_net_sol=metrics.team_net_sol if metrics else None,
            coordinated_sell_count=metrics.coordinated_sell_count if metrics else 0,
            distribution_signal=signal,
        )

        conn.execute(
            """INSERT INTO post_grad_behavior
               (token_mint, checked_at, check_offset_h, holders_remaining_count,
                team_sold_pct, snipers_sold_pct, liquidity_usd,
                team_buy_count, team_sell_count, team_net_sol, coordinated_sell_count,
                distribution_signal)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(token_mint, check_offset_h) DO UPDATE SET
                   checked_at              = excluded.checked_at,
                   holders_remaining_count = excluded.holders_remaining_count,
                   team_sold_pct           = excluded.team_sold_pct,
                   snipers_sold_pct        = excluded.snipers_sold_pct,
                   liquidity_usd           = excluded.liquidity_usd,
                   team_buy_count          = excluded.team_buy_count,
                   team_sell_count         = excluded.team_sell_count,
                   team_net_sol            = excluded.team_net_sol,
                   coordinated_sell_count  = excluded.coordinated_sell_count,
                   distribution_signal     = excluded.distribution_signal""",
            (
                behavior.token_mint, behavior.checked_at, behavior.check_offset_h,
                behavior.holders_remaining_count, behavior.team_sold_pct,
                behavior.snipers_sold_pct, behavior.liquidity_usd,
                behavior.team_buy_count, behavior.team_sell_count,
                behavior.team_net_sol, behavior.coordinated_sell_count,
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
            snipers_sold_pct=behavior.snipers_sold_pct,
            liquidity_usd=behavior.liquidity_usd,
            team_buy_count=behavior.team_buy_count,
            team_sell_count=behavior.team_sell_count,
            team_net_sol=behavior.team_net_sol,
            coordinated_sell_count=behavior.coordinated_sell_count,
        ))
        if team_swaps:
            from src.analyzer.smart_money import get_smart_money_wallets as _gsmw
            _sm_set = {w.address for w in _gsmw(conn)}
            asyncio.create_task(sb.post_grad_swaps_batch(
                token_mint=token_mint,
                swaps=[
                    {
                        "token_mint": token_mint,
                        "wallet_address": s.signer,
                        "side": s.side,
                        "sol_amount": s.sol_amount,
                        "token_amount": s.token_amount,
                        "price_sol": (s.sol_amount / s.token_amount) if s.token_amount else None,
                        "ts": s.timestamp,
                        "slot": s.slot,
                        "is_sniper": bool(is_bc_sniper and s.signer in team_addresses),
                        "is_team": s.signer in team_addresses,
                        "is_smart_money": s.signer in _sm_set,
                    }
                    for s in team_swaps
                ],
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


# ── post-grad swap helpers ──────────────────────────────────────────────────────

async def _fetch_dex_stats(
    token_mint: str,
) -> tuple[float | None, float | None, str | None]:
    """Return (liquidity_usd, price_usd, pair_address) from the top-liquidity pair."""
    import aiohttp
    url = f"https://api.dexscreener.com/latest/dex/tokens/{token_mint}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return None, None, None
                data = await resp.json()
        pairs = data.get("pairs") or []
        if not pairs:
            return None, None, None
        best = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd", 0) or 0)
        liq = (best.get("liquidity") or {}).get("usd")
        price = best.get("priceUsd")
        pair_address = best.get("pairAddress")
        return (
            float(liq) if liq is not None else None,
            float(price) if price is not None else None,
            pair_address,
        )
    except Exception:
        return None, None, None


async def _fetch_liquidity_usd(token_mint: str) -> float | None:
    """Current USD liquidity from DexScreener's highest-liquidity pair, or None."""
    liq, _, _ = await _fetch_dex_stats(token_mint)
    return liq


def _load_graduated_at(token_mint: str, conn) -> int:
    """Graduation timestamp for a token, or 0 if unknown."""
    row = conn.execute(
        "SELECT graduated_at FROM graduation_events WHERE token_mint = ?", (token_mint,)
    ).fetchone()
    return int(row["graduated_at"]) if row and row["graduated_at"] else 0


def _load_pool_accounts(token_mint: str, conn) -> set[str]:
    """Per-mint pool/curve accounts captured at graduation (pipeline_version ≥ 2)."""
    row = conn.execute(
        "SELECT pool_accounts_json FROM graduation_events WHERE token_mint = ?",
        (token_mint,),
    ).fetchone()
    if not row or not row["pool_accounts_json"]:
        return set()
    try:
        return {a for a in json.loads(row["pool_accounts_json"]) if isinstance(a, str)}
    except Exception:
        return set()


def _load_grad_positions(token_mint: str, conn) -> dict[str, float]:
    """Map wallet → token holding at graduation, from bc_top_holders_json."""
    row = conn.execute(
        "SELECT bc_top_holders_json FROM graduation_events WHERE token_mint = ?",
        (token_mint,),
    ).fetchone()
    if not row or not row["bc_top_holders_json"]:
        return {}
    try:
        holders = json.loads(row["bc_top_holders_json"])
    except Exception:
        return {}
    positions: dict[str, float] = {}
    for h in holders:
        wallet = h.get("wallet")
        ui = h.get("ui_amount")
        if wallet and ui:
            positions[wallet] = float(ui)
    return positions
