"""Pump.fun graduation monitor.

Detects when a token completes its bonding curve and migrates to PumpSwap.
Only tokens that graduate (~0.7-0.8% of all launches) receive structural
analysis — this is the primary quality filter.

Graduation = token raises ~85 SOL on Pump.fun bonding curve and is
automatically migrated to PumpSwap AMM (~$69K market cap at migration).

Detection strategy:
  Primary:  Pump.fun WebSocket event (event name assumed = 'migrate' — TODO: verify)
  Fallback: REST poll of pump.fun recently-graduated endpoint every 30 s

Post-detection pipeline (async, non-blocking):
  1. Record graduation_event to DB
  2. Fetch BC top-holders from Helius (who held at graduation?)
  3. Build team_cluster from BC-phase buyers + holder list
  4. Produce StructuralRead verdict
  5. Schedule distribution checks at +1h / +4h / +24h

Constants to verify against current deployment:
  PUMPSWAP_PROGRAM_ID — check Solscan for current PumpSwap AMM program
  GRADUATION_EVENT    — actual Socket.IO event name emitted at graduation
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass

import aiohttp

from src.common.config import settings
from src.common.db import get_connection
from src.common.models import GraduationEvent, TeamCluster, TokenBuyer

logger = logging.getLogger(__name__)

# TODO: verify this program ID matches the live PumpSwap AMM deployment
PUMPSWAP_PROGRAM_ID = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
PUMPPORTAL_WS = "wss://pumpportal.fun/api/data"
PUMP_FUN_API = "https://frontend-api.pump.fun"  # REST fallback only

MIN_GRADUATION_MC_USD = 50_000.0   # sanity-check lower bound

# Wait for ST/DexScreener to index the migration AMM pool before analysing
_POOL_INDEX_DELAY_S = 45

# Watchdog: alert once per episode via Telegram when the feed goes quiet or
# analyses fail repeatedly — silent failure previously went unnoticed for weeks.
_STALL_ALERT_S = 3600          # no graduation for 1h (normal cadence: 5-13/h)
_FAILURE_ALERT_THRESHOLD = 5   # consecutive analysis failures
_WATCHDOG_CHECK_S = 300


class _Watchdog:
    def __init__(self) -> None:
        self.last_event_ts = time.time()
        self.consecutive_failures = 0
        self._stall_alerted = False
        self._failure_alerted = False

    def beat(self) -> None:
        self.last_event_ts = time.time()
        self._stall_alerted = False

    def analysis_ok(self) -> None:
        self.consecutive_failures = 0
        self._failure_alerted = False

    def analysis_failed(self) -> None:
        self.consecutive_failures += 1

    async def check(self) -> None:
        from src.notifications.telegram import send_message
        quiet_s = time.time() - self.last_event_ts
        if quiet_s > _STALL_ALERT_S and not self._stall_alerted:
            self._stall_alerted = True
            await send_message(
                f"⚠️ graduation feed quiet for {quiet_s/3600:.1f}h — "
                "check PumpPortal WS / Solana Tracker credits / logs"
            )
        if self.consecutive_failures >= _FAILURE_ALERT_THRESHOLD and not self._failure_alerted:
            self._failure_alerted = True
            await send_message(
                f"⚠️ {self.consecutive_failures} consecutive graduation analyses failed — "
                "check logs/graduation_monitor.err (API credits? schema?)"
            )


_watchdog = _Watchdog()


async def _watchdog_loop() -> None:
    while True:
        await asyncio.sleep(_WATCHDOG_CHECK_S)
        try:
            await _watchdog.check()
        except Exception:
            pass
        try:
            await _sync_api_usage()
        except Exception:
            pass


async def _sync_api_usage() -> None:
    """Push today's + yesterday's API-usage counts to Supabase (System page)."""
    from src.common.api_usage import flush
    from src.common import supabase_sync as sb
    flush()   # ensure pending in-memory counts are persisted first
    conn = get_connection()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT day, provider, endpoint, count FROM api_usage "
            "WHERE day >= date('now','-1 day')"
        )]
    finally:
        conn.close()
    if rows:
        await sb.api_usage_batch(rows)


# ── internal event struct ─────────────────────────────────────────────────────

@dataclass
class _MigrateEvent:
    mint: str
    pool_address: str | None
    event_ts: int


def _parse_migrate(raw: dict) -> _MigrateEvent | None:
    """Parse a raw Socket.IO migrate payload; field names are best-guess."""
    mint = raw.get("mint") or raw.get("token") or raw.get("address")
    if not mint:
        logger.debug("migrate event missing mint — keys: %s", list(raw.keys()))
        return None
    pool = raw.get("pool") or raw.get("pool_address") or raw.get("ammPool")
    ts = int(raw.get("timestamp") or time.time())
    return _MigrateEvent(mint=str(mint), pool_address=pool, event_ts=ts)


# ── monitor ───────────────────────────────────────────────────────────────────

class GraduationMonitor:
    """Connects to PumpPortal WebSocket and triggers structural analysis on graduation."""

    def __init__(self) -> None:
        self._seen: set[str] = set()

    async def run(self) -> None:
        """Connect and run forever; starts the REST poll fallback in background."""
        asyncio.create_task(self._poll_fallback())
        while True:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(
                        PUMPPORTAL_WS,
                        heartbeat=30,
                        timeout=aiohttp.ClientTimeout(total=None, connect=15),
                    ) as ws:
                        logger.info("graduation monitor connected to PumpPortal WS")
                        await ws.send_json({"method": "subscribeMigration"})

                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                try:
                                    raw = json.loads(msg.data)
                                    event = _parse_migrate(raw)
                                    if event and event.mint not in self._seen:
                                        self._seen.add(event.mint)
                                        _watchdog.beat()
                                        lag = max(0, int(time.time()) - event.event_ts)
                                        logger.info(
                                            "graduation WS: %s (lag %ds)",
                                            event.mint[:8], lag,
                                        )
                                        asyncio.create_task(
                                            _handle_graduation(
                                                event.mint, event.pool_address, lag
                                            )
                                        )
                                except Exception:
                                    pass
                            elif msg.type in (
                                aiohttp.WSMsgType.CLOSED,
                                aiohttp.WSMsgType.ERROR,
                            ):
                                break

                        logger.warning("graduation WS closed — reconnecting")
            except Exception as exc:
                logger.warning("graduation WS error: %s — retrying", exc)
            await asyncio.sleep(5)

    async def _poll_fallback(self) -> None:
        """Poll REST endpoint every 30 s to catch graduations missed by WS."""
        url = f"{PUMP_FUN_API}/coins/recently-graduated"
        while True:
            await asyncio.sleep(30)
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        url, timeout=aiohttp.ClientTimeout(total=10)
                    ) as resp:
                        if resp.status != 200:
                            continue
                        coins = await resp.json()
                        for coin in (coins if isinstance(coins, list) else []):
                            mint = coin.get("mint")
                            if not mint or mint in self._seen:
                                continue
                            self._seen.add(mint)
                            pool = (
                                coin.get("raydiumPool")
                                or coin.get("pumpAmmPool")
                                or coin.get("pool")
                            )
                            # Try known timestamp field names; -1 = detected via REST poll
                            # (graduation timestamp unknown — can't compute true lag)
                            raw_ts = (
                                coin.get("last_updated")
                                or coin.get("created_timestamp")
                                or coin.get("timestamp")
                            )
                            lag = abs(int(time.time()) - int(raw_ts)) if raw_ts else -1
                            logger.info("graduation poll: %s (lag %s)", mint[:8], f"{lag}s" if lag >= 0 else "unknown")
                            asyncio.create_task(_handle_graduation(mint, pool, lag))
            except Exception as exc:
                logger.warning("graduation poll error: %s", exc)


# ── pipeline ──────────────────────────────────────────────────────────────────

async def _handle_graduation(
    mint: str,
    pool_address: str | None,
    detection_lag: int,
) -> None:
    """Persist the graduation and run full structural analysis.

    NOTE: `pool_address` from the PumpPortal migrate payload is a VENUE LABEL
    ('pump-amm' / 'raydium-cpmm'), not an address — stored as migration_venue.
    The real AMM pool accounts come from the token-info response.
    """
    from src.ingest.solana_tracker import SolanaTrackerClient
    from src.analyzer.structural_accounts import (
        structural_set, extract_pool_accounts, extract_total_supply, extract_market_state,
    )
    from src.analyzer.project_classifier import (
        _extract_meta_fields, extract_creation,
    )
    from src.common.cex_wallets import get_all_cex_addresses

    # Give Solana Tracker / DexScreener time to index the freshly created AMM
    # pool — fetching at T+0 misses it in pools[] and the pool then pollutes
    # top holders (observed live: pool as 42% "top holder"). Analysis quality
    # at +45s is identical; distribution checks are at 1h+ anyway.
    await asyncio.sleep(_POOL_INDEX_DELAY_S)

    conn = get_connection()
    try:
        now = int(time.time())

        # Token-info FIRST: it carries the real created_time (fixes BC-window
        # reconstruction), creator wallet, pool accounts, total supply, and the
        # classification metadata — one request serving five consumers.
        async with SolanaTrackerClient() as st:
            try:
                token_info = await st.get_token_info(mint)
            except Exception as exc:
                logger.warning("token-info fetch failed for %s: %s", mint[:8], exc)
                token_info = None

            meta = _extract_meta_fields(mint, token_info)
            creator_wallet, created_time = extract_creation(token_info)
            pool_accounts = extract_pool_accounts(token_info)
            total_supply = extract_total_supply(token_info)
            # NON-RECOVERABLE point-in-time market + holder state (zero extra API)
            market = extract_market_state(token_info)

            token_row = conn.execute(
                "SELECT mint, symbol, created_at FROM tokens WHERE mint = ?", (mint,)
            ).fetchone()

            # Launch-time seen by pump_monitor wins; token-info creation time next;
            # `now` only as a flagged last resort (corrupts bc_duration otherwise).
            row_created = (
                int(token_row["created_at"])
                if token_row is not None and token_row["created_at"] else None
            )
            if row_created and row_created < now - 300 and (
                not created_time or row_created <= created_time + 60
            ):
                token_created_at, created_at_source = row_created, "launch_ws"
            elif created_time:
                token_created_at, created_at_source = created_time, "token_info"
            else:
                token_created_at, created_at_source = now, "fallback_now"

            if token_row is None:
                symbol = meta.symbol or "UNKNOWN"
                name = meta.name or "Unknown"
                if symbol == "UNKNOWN":
                    ds_symbol, ds_name = await _dexscreener_symbol_name(mint)
                    symbol, name = ds_symbol or "UNKNOWN", ds_name or "Unknown"
                conn.execute(
                    """INSERT OR IGNORE INTO tokens
                       (mint, symbol, name, launchpad, created_at, narrative_tags)
                       VALUES (?, ?, ?, 'pump.fun', ?, '[]')""",
                    (mint, symbol, name, token_created_at),
                )
                from src.common import supabase_sync as sb
                asyncio.create_task(sb.token(
                    mint, symbol, name, token_created_at,
                    created_at_source=created_at_source,
                    creator_wallet=creator_wallet,
                    total_supply=total_supply,
                ))
            else:
                symbol = token_row["symbol"] or "?"

            conn.execute(
                """UPDATE tokens SET created_at = ?, created_at_source = ?,
                       creator_wallet = COALESCE(?, creator_wallet),
                       total_supply = ?
                   WHERE mint = ?""",
                (token_created_at, created_at_source, creator_wallet,
                 total_supply, mint),
            )
            conn.commit()

            # token-info pools[] at graduation still shows the BONDING CURVE pool
            # (the new AMM pool isn't indexed yet). DexScreener's pairAddress is
            # the AMM pool when already listed; otherwise the 1h distribution
            # check backfills it (see distribution._persist_pool_account).
            ds_pairs = await _dexscreener_pair_addresses(mint)
            pool_accounts |= ds_pairs
            amm_pool_address = next(iter(sorted(ds_pairs)), None)

            conn.execute(
                """INSERT OR REPLACE INTO graduation_events
                   (token_mint, graduated_at, detection_lag_seconds,
                    pumpswap_pool_address, migration_venue, amm_pool_address,
                    pool_accounts_json, pipeline_version, bc_top_holders_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 2, '[]')""",
                (mint, now, detection_lag, amm_pool_address, pool_address,
                 amm_pool_address, json.dumps(sorted(pool_accounts))),
            )
            conn.execute(
                """INSERT OR REPLACE INTO graduation_market
                   (token_mint, captured_at, holder_count, liquidity_usd,
                    market_cap_usd, price_usd, txns_buys, txns_sells, txns_total)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (mint, now, market["holder_count"], market["liquidity_usd"],
                 market["market_cap_usd"], market["price_usd"],
                 market["txns_buys"], market["txns_sells"], market["txns_total"]),
            )
            conn.commit()

            # Holders at graduation, with pool/curve/program accounts excluded so
            # they never surface as "top holders" or team members.
            structural = structural_set(token_info, get_all_cex_addresses(conn))
            accounts = await st.get_token_holders(mint)
            accounts = [a for a in accounts if a.get("address") not in structural]
            bc_top_holders = _parse_bc_holders(accounts, total_supply)
            bc_swaps = await _reconstruct_bc(
                st, mint, bc_top_holders, token_created_at, now, conn,
                structural=structural,
            )

        # The tape is ground truth for launch time — ST's created_time is
        # sometimes the indexing moment (≈ graduation), corrupting bc_duration
        # and first-buy offsets. First trade wins when it's meaningfully earlier.
        if bc_swaps:
            first_trade_ts = min(s.timestamp for s in bc_swaps)
            if first_trade_ts < token_created_at - 60:
                token_created_at = first_trade_ts
                conn.execute(
                    "UPDATE tokens SET created_at = ?, created_at_source = 'first_trade' WHERE mint = ?",
                    (token_created_at, mint),
                )
                conn.commit()

        # Funding + wallet age for the top holders (free RPC, cached), then
        # launch coordination WITH funder/fresh maps — shared-funder edges and
        # fresh-wallet ratios were structurally zero before this.
        top_wallets = [h["wallet"] for h in bc_top_holders[:_BC_RECONSTRUCT_TOP_N] if h.get("wallet")]
        funder_by_wallet, first_seen_map = await _resolve_wallet_funding(top_wallets, conn)

        # Slot-level microstructure: resolve real slots for the earliest buys and
        # remap Swap.slot (second-proxy → real slot) so same-slot coordination
        # becomes true same-BLOCK bundling. Runs before coordination.
        micro_slot_by_sig, micro_features = await _resolve_microstructure(mint, bc_swaps, conn)
        if micro_slot_by_sig:
            for s in bc_swaps:
                real = micro_slot_by_sig.get(s.tx_signature)
                if real is not None:
                    s.slot = real

        fresh_map: dict[str, str] = {}
        if first_seen_map:
            from src.analyzer.coordination import fresh_flags
            first_buy_offset = {}
            for s in bc_swaps:
                if s.side == "buy":
                    off = float(max(0, s.timestamp - token_created_at))
                    prev = first_buy_offset.get(s.signer)
                    if prev is None or off < prev:
                        first_buy_offset[s.signer] = off
            fresh_map = fresh_flags(first_seen_map, first_buy_offset, now)
        # Behavioral-similarity vectors for this coin's buyers (Phase C) — catches
        # teams that rotate wallets AND funders but keep operational habits.
        from src.analyzer.wallet_behavior import load_behavior_vectors
        behavior_vectors = load_behavior_vectors(
            [s.signer for s in bc_swaps if s.side == "buy"], conn
        )
        _detect_launch_coordination(
            mint, bc_swaps, conn, total_supply=total_supply,
            funder_by_wallet=funder_by_wallet, fresh=fresh_map,
            real_slots=bool(micro_slot_by_sig), behavior_vectors=behavior_vectors,
        )

        if bc_swaps:
            from src.analyzer.flow_features import (
                compute_bc_flow_features, upsert_bc_flow_features,
            )
            upsert_bc_flow_features(
                conn, mint, compute_bc_flow_features(bc_swaps, token_created_at)
            )
            if micro_features is not None:
                from src.analyzer.microstructure import upsert_micro_features
                upsert_micro_features(conn, mint, micro_features)
            # Mirror the full flow+microstructure row to Supabase (dashboard)
            ff = conn.execute(
                "SELECT * FROM bc_flow_features WHERE token_mint = ?", (mint,)
            ).fetchone()
            if ff:
                from src.common import supabase_sync as sb
                row = {k: ff[k] for k in ff.keys() if k != "token_mint"}
                asyncio.create_task(sb.bc_flow_features(mint, row))

        conn.execute(
            "UPDATE graduation_events SET bc_top_holders_json = ? WHERE token_mint = ?",
            (json.dumps(bc_top_holders), mint),
        )
        conn.commit()

        # Load BC-phase buyers (now backfilled by _reconstruct_bc) from token_buyers
        rows = conn.execute(
            """SELECT wallet_address, sol_amount, tokens_received, bought_at
               FROM token_buyers WHERE token_mint = ?""",
            (mint,),
        ).fetchall()
        buyers = [
            TokenBuyer(
                token_mint=mint,
                wallet_address=r["wallet_address"],
                sol_amount=r["sol_amount"],
                tokens_received=r["tokens_received"],
                bought_at=r["bought_at"],
            )
            for r in rows
        ]

        grad_event = GraduationEvent(
            token_mint=mint,
            graduated_at=now,
            detection_lag_seconds=detection_lag,
            pumpswap_pool_address=amm_pool_address,
            bc_top_holders=bc_top_holders,
        )

        logger.info(
            "structural analysis: $%s — %d BC buyers, %d top holders",
            symbol, len(buyers), len(bc_top_holders),
        )

        await analyse_graduation(
            grad_event, buyers, conn, symbol=symbol,
            structural=structural, meta=meta,
        )
        _watchdog.analysis_ok()

    except Exception:
        _watchdog.analysis_failed()
        logger.exception("graduation analysis failed for %s", mint[:8])
    finally:
        conn.close()


async def analyse_graduation(
    event: GraduationEvent,
    buyers: list[TokenBuyer],
    conn,
    *,
    symbol: str = "?",
    structural: frozenset[str] = frozenset(),
    meta=None,
) -> None:
    """Full structural analysis pipeline for a graduated token."""
    from src.analyzer.team_detect import build_team_cluster_post_grad, upsert_team_members
    from src.analyzer.distribution import schedule_distribution_checks
    from src.analyzer.smart_money import (
        get_smart_money_wallets,
        find_smart_money_in_buyers,
        get_funder_reputation,
    )
    from src.strategy.rules import structural_read
    from src.common.cex_wallets import get_all_cex_addresses

    cex_addresses = get_all_cex_addresses(conn)

    # Ensure the creator's own funder is traced (cached) so the funded-by-creator
    # insider fingerprint can fire — creator is rarely in the top-holder set.
    creator_row = conn.execute(
        "SELECT creator_wallet FROM tokens WHERE mint = ?", (event.token_mint,)
    ).fetchone()
    if creator_row and creator_row["creator_wallet"]:
        await _resolve_wallet_funding([creator_row["creator_wallet"]], conn)

    # Assemble per-wallet evidence maps for probabilistic team scoring (all from
    # data already gathered this graduation — zero extra API).
    evidence = _gather_team_evidence(event.token_mint, buyers, conn)
    team_cluster, scored = build_team_cluster_post_grad(
        event.token_mint, buyers, event.bc_top_holders, cex_addresses,
        structural_addresses=structural, graduated_at=event.graduated_at,
        **evidence,
    )
    if scored:
        member_set = set(team_cluster.member_addresses) if team_cluster else set()
        upsert_team_members(conn, event.token_mint, scored, member_set)
        from src.common import supabase_sync as sb
        asyncio.create_task(sb.team_members_batch([
            {"token_mint": event.token_mint, "wallet": w, "score": sc,
             "is_member": int(w in member_set), "evidence_json": ev}
            for w, (sc, ev) in scored.items()
        ]))

    # F4a: resolve the team's funding source so funder_reputation can populate
    if team_cluster and not team_cluster.funding_source:
        team_cluster.funding_source = await _resolve_funding_source(
            team_cluster.member_addresses, conn
        )

    if team_cluster:
        conn.execute(
            """INSERT OR REPLACE INTO team_clusters
               (cluster_id, token_mint, funding_source, member_addresses,
                supply_pct_at_graduation, first_buy_offset_seconds, is_bc_sniper)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                team_cluster.cluster_id,
                team_cluster.token_mint,
                team_cluster.funding_source,
                json.dumps(team_cluster.member_addresses),
                team_cluster.supply_pct_at_graduation,
                team_cluster.first_buy_offset_seconds,
                int(team_cluster.is_bc_sniper),
            ),
        )
        conn.commit()

    smart_money_list = get_smart_money_wallets(conn)
    sm_buyers = find_smart_money_in_buyers(buyers, smart_money_list)

    funder_rep = None
    if team_cluster and team_cluster.funding_source:
        funder_rep = get_funder_reputation(team_cluster.funding_source, conn)

    # Serial-deployer signal (creator captured at graduation from token-info)
    from src.analyzer.smart_money import get_creator_reputation
    creator_row = conn.execute(
        "SELECT creator_wallet FROM tokens WHERE mint = ?", (event.token_mint,)
    ).fetchone()
    creator_rep = get_creator_reputation(
        creator_row["creator_wallet"] if creator_row else None, conn
    )

    from src.analyzer.team_memory import gather_memory_signals
    memory_signals = gather_memory_signals(team_cluster, conn)

    # ── Graduation push detection (from already-fetched data, no extra API calls) ──
    token_created_at = conn.execute(
        "SELECT created_at FROM tokens WHERE mint = ?", (event.token_mint,)
    ).fetchone()
    bc_duration_seconds = (
        event.graduated_at - int(token_created_at["created_at"])
        if token_created_at and token_created_at["created_at"]
        else -1
    )
    top_holder_pct = float(event.bc_top_holders[0]["pct"]) if event.bc_top_holders else 0.0
    top3_holder_pct = sum(float(h["pct"]) for h in event.bc_top_holders[:3])
    unique_bc_buyers = len(set(b.wallet_address for b in buyers))
    proven_wallet_count = _count_proven_buyers(buyers, conn)

    # Behavioral signals (Phases B/D) for the verdict + snapshot
    micro_row = conn.execute(
        "SELECT launch_slot_snipe_count, max_same_slot_group, bundled_adjacent_count "
        "FROM bc_flow_features WHERE token_mint = ?", (event.token_mint,)
    ).fetchone()
    launch_slot_snipe_count = int(micro_row["launch_slot_snipe_count"] or 0) if micro_row else 0
    leader_consistency = choreo_n = None
    if team_cluster and team_cluster.funding_source:
        fp = conn.execute(
            "SELECT leader_consistency, choreography_sample_count "
            "FROM team_fingerprints WHERE funding_source = ?",
            (team_cluster.funding_source,),
        ).fetchone()
        if fp:
            leader_consistency = fp["leader_consistency"]
            choreo_n = fp["choreography_sample_count"]

    team_scores = [sc for sc, _ in scored.values()] if scored else []

    ctx = {
        "token_mint": event.token_mint,
        "team_cluster": team_cluster,
        "funder_rep": funder_rep,
        "smart_money_count": len(sm_buyers),
        "bc_top_holders": event.bc_top_holders,
        "distribution_signal": None,
        "bundle_pct": getattr(team_cluster, "supply_pct_at_graduation", 0.0) if team_cluster else 0.0,
        "memory_signals": memory_signals,
        "bc_duration_seconds": bc_duration_seconds,
        "top_holder_pct": top_holder_pct,
        "top3_holder_pct": top3_holder_pct,
        "unique_bc_buyers": unique_bc_buyers,
        "proven_wallet_count": proven_wallet_count,
        "creator_rep": creator_rep,
        "launch_slot_snipe_count": launch_slot_snipe_count,
        "funder_leader_consistency": leader_consistency,
        "funder_choreography_n": choreo_n,
        "team_score_max": max(team_scores) if team_scores else 0.0,
        "team_score_mean": round(sum(team_scores) / len(team_scores), 4) if team_scores else 0.0,
        "scored_member_count": len(team_cluster.member_addresses) if team_cluster else 0,
    }
    read = structural_read(ctx)

    # Persist verdict to SQLite so it's available for local queries
    conn.execute(
        """UPDATE graduation_events
           SET structural_verdict = ?, verdict_confidence = ?,
               smart_money_count = ?, dominant_factors_json = ?
           WHERE token_mint = ?""",
        (
            read.verdict, read.confidence,
            len(sm_buyers), json.dumps(read.dominant_factors),
            event.token_mint,
        ),
    )
    conn.commit()

    _snapshot_features(event, ctx, read, conn)

    _print_graduation_alert(event, symbol, team_cluster, sm_buyers, funder_rep, read)

    # Sync to Supabase (fire-and-forget — never blocks analysis)
    from src.common import supabase_sync as sb
    asyncio.create_task(sb.graduation_event(
        token_mint=event.token_mint,
        graduated_at=event.graduated_at,
        detection_lag_seconds=event.detection_lag_seconds,
        structural_verdict=read.verdict,
        verdict_confidence=read.confidence,
        pumpswap_pool_address=event.pumpswap_pool_address,
        bc_top_holders_json=event.bc_top_holders,
        smart_money_count=len(sm_buyers),
        dominant_factors_json=read.dominant_factors,
        pipeline_version=2,
    ))
    if team_cluster:
        asyncio.create_task(sb.team_cluster(
            cluster_id=team_cluster.cluster_id,
            token_mint=team_cluster.token_mint,
            funding_source=team_cluster.funding_source,
            member_addresses=team_cluster.member_addresses,
            supply_pct_at_graduation=team_cluster.supply_pct_at_graduation,
            first_buy_offset_seconds=team_cluster.first_buy_offset_seconds,
            is_bc_sniper=team_cluster.is_bc_sniper,
        ))

    # Classify project-vs-meme + alert on real projects (fire-and-forget, non-fatal)
    asyncio.create_task(_classify_and_notify(event.token_mint, symbol, read, conn, meta=meta))

    # Schedule price outcome checks at 1h / 4h / 24h from graduation
    # Baseline: ~$69K USD — Pump.fun bonding curve always migrates near this MC
    from src.analyzer.outcome_tracker import schedule_checks
    GRADUATION_MC_USD = 69_000.0
    asyncio.create_task(schedule_checks(event.token_mint, GRADUATION_MC_USD))

    # Schedule distribution checks (team behavior) at 1h / 4h / 24h
    await schedule_distribution_checks(event.token_mint, event.graduated_at)


async def _classify_and_notify(token_mint: str, symbol: str, read, conn, meta=None) -> None:
    """Classify project vs meme; store it; alert on project AND verdict != SKIP.

    `meta` is the already-extracted token-info metadata from _handle_graduation
    (saves one API request); fetched fresh only when absent (e.g. backfills).
    """
    from src.analyzer.project_classifier import (
        fetch_token_meta, fill_meta_gaps, classify_project, _is_real_website,
    )
    from src.common import supabase_sync as sb
    try:
        if meta is not None:
            meta = await fill_meta_gaps(meta)
        else:
            from src.ingest.solana_tracker import SolanaTrackerClient
            async with SolanaTrackerClient() as st:
                meta = await fetch_token_meta(token_mint, st)
        cls = await classify_project(meta)

        own = get_connection()
        try:
            own.execute(
                """INSERT INTO token_classification
                       (token_mint, label, is_project, confidence, reason, has_website,
                        website, twitter, telegram, description, computed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(token_mint) DO UPDATE SET
                       label=excluded.label, is_project=excluded.is_project,
                       confidence=excluded.confidence, reason=excluded.reason,
                       has_website=excluded.has_website, website=excluded.website,
                       twitter=excluded.twitter, telegram=excluded.telegram,
                       description=excluded.description, computed_at=excluded.computed_at""",
                (
                    token_mint, cls.label, int(cls.is_project), cls.confidence, cls.reason,
                    int(_is_real_website(meta.website)), meta.website, meta.twitter,
                    meta.telegram, meta.description, int(time.time()),
                ),
            )
            own.commit()
        finally:
            own.close()

        asyncio.create_task(sb.token_classification(
            token_mint=token_mint, label=cls.label, is_project=cls.is_project,
            confidence=cls.confidence, reason=cls.reason,
            has_website=_is_real_website(meta.website), website=meta.website,
            twitter=meta.twitter, telegram=meta.telegram, description=meta.description,
        ))

        logger.info("classify %s — %s (%.0f%%) %s", token_mint[:8], cls.label,
                    cls.confidence * 100, cls.reason[:60] if cls.reason else "")

        if cls.is_project and read.verdict != "SKIP":
            coord = conn.execute(
                """SELECT bundled_supply_pct, largest_entity_supply_pct
                   FROM coin_coordination WHERE token_mint = ? AND phase = 'launch'""",
                (token_mint,),
            ).fetchone()
            from src.notifications.telegram import notify_project_graduation
            await notify_project_graduation(
                symbol=symbol, name=meta.name or symbol, mint=token_mint,
                description=meta.description, website=meta.website,
                twitter=meta.twitter, telegram=meta.telegram,
                verdict=read.verdict, confidence=read.confidence,
                bundled_supply_pct=coord["bundled_supply_pct"] if coord else None,
                largest_entity_supply_pct=coord["largest_entity_supply_pct"] if coord else None,
            )
    except Exception:
        logger.exception("classify/notify failed for %s", token_mint[:8])


# ── helpers ───────────────────────────────────────────────────────────────────

_BC_RECONSTRUCT_TOP_N = 8   # cap holders reconstructed per graduation (Helius budget)


async def _reconstruct_bc(
    helius, mint: str, bc_top_holders: list[dict],
    token_created_at: int, graduated_at: int, conn,
    structural: frozenset[str] = frozenset(),
) -> list:
    """F1: reconstruct BC accumulation for top holders + backfill token_buyers.

    Returns the BC swap tape so the caller can run launch coordination AFTER
    funding resolution (funder/fresh maps make the entity clustering real).
    """
    from src.analyzer.bc_reconstruct import (
        reconstruct_bc_holders, upsert_bc_accumulation, to_token_buyers,
    )
    wallets = [h["wallet"] for h in bc_top_holders[:_BC_RECONSTRUCT_TOP_N] if h.get("wallet")]
    if not wallets:
        return []
    try:
        profiles, bc_swaps = await reconstruct_bc_holders(
            helius, mint, wallets, token_created_at, graduated_at,
            structural=structural,
        )
    except Exception as exc:
        logger.debug("BC reconstruction failed for %s: %s", mint[:8], exc)
        return []

    upsert_bc_accumulation(conn, mint, profiles)

    # Backfill token_buyers so build_team_cluster_post_grad gets real overlap
    buyers = to_token_buyers(bc_swaps, mint)
    for b in buyers:
        conn.execute(
            """INSERT OR IGNORE INTO wallets (address) VALUES (?)""",
            (b.wallet_address,),
        )
        conn.execute(
            """INSERT INTO token_buyers
                   (token_mint, wallet_address, bought_at, sol_amount, tokens_received)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT DO NOTHING""",
            (b.token_mint, b.wallet_address, b.bought_at, b.sol_amount, b.tokens_received),
        )
    conn.commit()

    logger.info(
        "BC reconstruct %s — %d holders profiled, %d buyers backfilled",
        mint[:8], len(profiles), len(buyers),
    )

    from src.common import supabase_sync as sb
    asyncio.create_task(sb.bc_accumulation_batch(
        token_mint=mint,
        rows=[
            {
                "token_mint": mint,
                "wallet_address": p.wallet,
                "first_buy_offset_s": p.first_buy_offset_s,
                "bc_buy_count": p.bc_buy_count,
                "bc_sell_count": p.bc_sell_count,
                "total_sol_in": p.total_sol_in,
                "accumulation_style": p.accumulation_style,
            }
            for p in profiles.values()
        ],
    ))

    return bc_swaps


def _detect_launch_coordination(
    mint: str, bc_swaps, conn,
    total_supply: float | None = None,
    funder_by_wallet: dict[str, str | None] | None = None,
    fresh: dict[str, str] | None = None,
    real_slots: bool = False,
    behavior_vectors: dict[str, tuple[float, ...]] | None = None,
) -> None:
    """Run the coordination engine on bonding-curve swaps; store phase='launch'.

    funder_by_wallet + fresh unlock shared-funder edges and fresh-wallet ratios
    in the entity clustering (previously never passed — always empty).
    real_slots=True means Swap.slot carries true block slots (Phase B remap), so
    same-slot edges are genuine same-block bundles — labeled same_slot_real.
    """
    from src.analyzer.coordination import analyze_coin, upsert_coordination
    if not bc_swaps:
        return
    try:
        cc = analyze_coin(
            mint, bc_swaps, total_supply=total_supply,
            funder_by_wallet=funder_by_wallet, fresh=fresh,
            behavior_vectors=behavior_vectors, same_slot_real=real_slots,
        )
        upsert_coordination(conn, cc, source="live", phase="launch")
    except Exception as exc:
        logger.debug("launch coordination failed for %s: %s", mint[:8], exc)
        return

    logger.info(
        "launch coordination %s — %d entities, %.1f%% bundled, largest %dw",
        mint[:8], cc.entity_count, cc.bundle_stats.bundled_supply_pct,
        cc.largest_entity_wallet_count,
    )

    from src.common import supabase_sync as sb
    asyncio.create_task(sb.coin_coordination(
        token_mint=mint, entity_count=cc.entity_count,
        bundled_supply_pct=cc.bundle_stats.bundled_supply_pct,
        bundle_wallet_count=cc.bundle_stats.bundle_wallet_count,
        largest_bundle_size=cc.bundle_stats.largest_bundle_size,
        largest_entity_supply_pct=cc.largest_entity_supply_pct,
        largest_entity_wallet_count=cc.largest_entity_wallet_count,
        largest_entity_fresh_ratio=cc.largest_entity_fresh_ratio,
        largest_entity_state=cc.largest_entity_state, phase="launch",
    ))
    if cc.entities:
        import time as _t
        asyncio.create_task(sb.coordinated_entities_batch(
            token_mint=mint,
            rows=[
                {
                    "token_mint": mint, "phase": "launch", "entity_id": e.entity_id,
                    "member_addresses": list(e.wallets), "wallet_count": e.wallet_count,
                    "supply_pct": e.supply_pct, "fresh_ratio": e.fresh_ratio,
                    "state": e.state, "edge_sources": list(e.edge_sources),
                    "computed_at": int(_t.time()),
                }
                for e in cc.entities
            ],
        ))


_FUNDER_RESOLVE_MAX_MEMBERS = 12    # funding lookups per graduation (free RPC)
_FRESH_FUNDER_SIG_COUNT = 10        # funder with fewer sigs → peel one more hop


def _persist_funding_info(conn, info) -> None:
    """wallet_funding row + wallets.first_seen/funding_source (COALESCE-preserving)."""
    conn.execute(
        """INSERT INTO wallet_funding
               (wallet, hop, funder, sol_amount, funded_at, tx_signature,
                sig_count, traced_at)
           VALUES (?, 1, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(wallet, hop) DO UPDATE SET
               funder       = COALESCE(wallet_funding.funder, excluded.funder),
               sol_amount   = COALESCE(wallet_funding.sol_amount, excluded.sol_amount),
               funded_at    = COALESCE(wallet_funding.funded_at, excluded.funded_at),
               tx_signature = COALESCE(wallet_funding.tx_signature, excluded.tx_signature),
               sig_count    = MAX(wallet_funding.sig_count, excluded.sig_count),
               traced_at    = excluded.traced_at""",
        (
            info.wallet, info.funder,
            (info.lamports / 1e9) if info.lamports else None,
            info.funded_at, info.tx_signature, info.sig_count, int(time.time()),
        ),
    )
    conn.execute(
        """INSERT INTO wallets (address, first_seen, funding_source) VALUES (?, ?, ?)
           ON CONFLICT(address) DO UPDATE SET
               first_seen     = COALESCE(wallets.first_seen, excluded.first_seen),
               funding_source = COALESCE(wallets.funding_source, excluded.funding_source)""",
        (info.wallet, info.first_seen, info.funder),
    )


async def _resolve_wallet_funding(
    wallets: list[str], conn
) -> tuple[dict[str, str | None], dict[str, int]]:
    """Trace first funder + wallet age for `wallets` (free RPC, cached in
    wallet_funding). Fresh funders (<10 sigs) get their own funder traced too,
    stored as their own hop-1 row — the funding graph emerges from traversal.

    Returns (funder_by_wallet, first_seen_by_wallet).
    """
    from src.ingest.rpc import RpcClient, extract_funding_info_rpc

    funder_by_wallet: dict[str, str | None] = {}
    first_seen: dict[str, int] = {}
    todo = list(dict.fromkeys(wallets))[:_FUNDER_RESOLVE_MAX_MEMBERS]
    if not todo or not settings.rpc_url:
        return funder_by_wallet, first_seen

    # Cache hits first — wallet_funding survives across graduations
    uncached: list[str] = []
    for addr in todo:
        row = conn.execute(
            """SELECT wf.funder, w.first_seen
               FROM wallet_funding wf LEFT JOIN wallets w ON w.address = wf.wallet
               WHERE wf.wallet = ? AND wf.hop = 1""",
            (addr,),
        ).fetchone()
        if row:
            funder_by_wallet[addr] = row["funder"]
            if row["first_seen"]:
                first_seen[addr] = int(row["first_seen"])
        else:
            uncached.append(addr)

    if uncached:
        # RPC phase first, DB writes after — writing inside the loop would hold
        # a SQLite write transaction open across slow awaits and lock out every
        # other service ("database is locked" storms).
        gathered: list = []
        try:
            async with RpcClient() as rpc:
                for addr in uncached:
                    try:
                        info = await extract_funding_info_rpc(rpc, addr)
                    except Exception:
                        continue
                    funder_by_wallet[addr] = info.funder
                    if info.first_seen:
                        first_seen[addr] = info.first_seen
                    gathered.append(info)
                    # Hop 2: fresh intermediary funder → trace who funded IT
                    f = info.funder
                    if f and f != "cex" and not conn.execute(
                        "SELECT 1 FROM wallet_funding WHERE wallet = ? AND hop = 1", (f,)
                    ).fetchone():
                        try:
                            gathered.append(await extract_funding_info_rpc(
                                rpc, f, fresh_gate=_FRESH_FUNDER_SIG_COUNT,
                            ))
                        except Exception:
                            pass
        except Exception as exc:
            logger.debug("funding resolution failed: %s", exc)
        try:
            for info in gathered:
                _persist_funding_info(conn, info)
            conn.commit()
        except Exception as exc:
            logger.debug("funding persistence failed: %s", exc)

    return funder_by_wallet, first_seen


def _gather_team_evidence(mint: str, buyers, conn) -> dict:
    """Build the per-wallet evidence maps for score_team_membership from data
    already persisted this graduation (launch entities, funding, freshness,
    microstructure). Also resolves the creator's own funder (cached)."""
    # launch coordination entities → wallet → edge_sources
    entity_edges: dict[str, set[str]] = {}
    for row in conn.execute(
        """SELECT member_addresses, edge_sources FROM coordinated_entities
           WHERE token_mint = ? AND phase = 'launch'""",
        (mint,),
    ):
        try:
            members = json.loads(row["member_addresses"] or "[]")
            srcs = set(json.loads(row["edge_sources"] or "[]"))
        except Exception:
            continue
        for w in members:
            entity_edges.setdefault(w, set()).update(srcs)

    # funding + freshness for all buyers (cached in wallet_funding / wallets)
    addrs = list({b.wallet_address for b in buyers} | set(entity_edges))
    funder_by_wallet: dict[str, str | None] = {}
    first_seen: dict[str, int] = {}
    sig_count: dict[str, int] = {}
    if addrs:
        placeholders = ",".join("?" * len(addrs))
        for row in conn.execute(
            f"""SELECT wf.wallet, wf.funder, wf.sig_count, w.first_seen
                FROM wallet_funding wf LEFT JOIN wallets w ON w.address = wf.wallet
                WHERE wf.hop = 1 AND wf.wallet IN ({placeholders})""",
            addrs,
        ):
            funder_by_wallet[row["wallet"]] = row["funder"]
            if row["sig_count"] is not None:
                sig_count[row["wallet"]] = int(row["sig_count"])
            if row["first_seen"] is not None:
                first_seen[row["wallet"]] = int(row["first_seen"])

    # min slot_offset per wallet (Phase B microstructure)
    slot_offset: dict[str, int] = {}
    for row in conn.execute(
        """SELECT wallet, MIN(slot_offset_from_first) mo FROM bc_microstructure
           WHERE token_mint = ? AND slot_offset_from_first IS NOT NULL
           GROUP BY wallet""",
        (mint,),
    ):
        slot_offset[row["wallet"]] = int(row["mo"])

    creator_row = conn.execute(
        "SELECT creator_wallet FROM tokens WHERE mint = ?", (mint,)
    ).fetchone()
    creator_wallet = creator_row["creator_wallet"] if creator_row else None
    creator_funder = None
    if creator_wallet:
        cf = conn.execute(
            "SELECT funder FROM wallet_funding WHERE wallet = ? AND hop = 1",
            (creator_wallet,),
        ).fetchone()
        creator_funder = cf["funder"] if cf else None

    return {
        "entity_edges": entity_edges,
        "funder_by_wallet": funder_by_wallet,
        "creator_wallet": creator_wallet,
        "creator_funder": creator_funder,
        "first_seen": first_seen,
        "sig_count": sig_count,
        "slot_offset": slot_offset,
    }


async def _resolve_microstructure(mint: str, bc_swaps, conn):
    """Resolve slot + intra-block order for the first N BC buys (free RPC).

    Returns (slot_by_tx_signature, MicroFeatures). RPC phase gathers, then one
    batched write — never a write txn across an await (SQLite-lock discipline).
    """
    from src.analyzer.microstructure import (
        resolve_microstructure, upsert_microstructure, MicroFeatures,
    )
    from src.ingest.rpc import RpcClient

    if not bc_swaps or not settings.rpc_url:
        return {}, None
    buys = sorted((s for s in bc_swaps if s.side == "buy"), key=lambda s: s.timestamp)
    try:
        async with RpcClient() as rpc:
            rows, feats, slot_by_sig = await resolve_microstructure(
                rpc, mint, buys, settings.microstructure_first_n_buys,
            )
    except Exception as exc:
        logger.debug("microstructure resolution failed for %s: %s", mint[:8], exc)
        return {}, None
    try:
        upsert_microstructure(conn, rows)
    except Exception as exc:
        logger.debug("microstructure persist failed for %s: %s", mint[:8], exc)
    if rows:
        logger.info(
            "microstructure %s — %d buys resolved, %d launch-slot snipes, %d bundled",
            mint[:8], len(rows), feats.launch_slot_snipe_count, feats.bundled_adjacent_count,
        )
    return slot_by_sig, (feats if rows else None)


async def _resolve_funding_source(member_addresses: list[str], conn) -> str | None:
    """F4a: the team's common SOL funder — majority non-CEX funder among members."""
    from collections import Counter

    funder_by_wallet, _ = await _resolve_wallet_funding(member_addresses, conn)
    funders = [f for f in funder_by_wallet.values() if f and f != "cex"]
    if not funders:
        return None
    return Counter(funders).most_common(1)[0][0]


def _snapshot_features(event: GraduationEvent, ctx: dict, read, conn) -> None:
    """Persist the exact feature vector structural_read saw — the leak-proof
    training input (never recomputed from data written after graduation)."""
    tc = ctx.get("team_cluster")
    fr = ctx.get("funder_rep")
    mem = ctx.get("memory_signals")
    cr = ctx.get("creator_rep") or {}
    features = {
        "team_size": len(tc.member_addresses) if tc else 0,
        "team_supply_pct": tc.supply_pct_at_graduation if tc else None,
        "team_is_bc_sniper": bool(tc.is_bc_sniper) if tc else None,
        "team_first_buy_offset_s": tc.first_buy_offset_seconds if tc else None,
        "funder_n": len(fr.graduated_mints) if fr else 0,
        "funder_rug_rate": fr.rug_rate if fr else None,
        "funder_moon_rate": fr.moon_rate if fr else None,
        "smart_money_count": ctx.get("smart_money_count"),
        "proven_wallet_count": ctx.get("proven_wallet_count"),
        "bc_duration_seconds": ctx.get("bc_duration_seconds"),
        "top_holder_pct": ctx.get("top_holder_pct"),
        "top3_holder_pct": ctx.get("top3_holder_pct"),
        "unique_bc_buyers": ctx.get("unique_bc_buyers"),
        "graph_hits": len(mem.graph_hits) if mem else 0,
        "graph_rug_hits": sum(
            1 for h in (mem.graph_hits if mem else []) if h.rug_co_appearances >= 2
        ),
        "fingerprint_distance": (
            mem.fingerprint_match.distance if mem and mem.fingerprint_match else None
        ),
        "launches_24h": mem.launches_24h if mem else 0,
        "launches_7d": mem.launches_7d if mem else 0,
        "expected_dump_start_h": mem.expected_dump_start_h if mem else None,
        "creator_n": cr.get("n", 0),
        "creator_rug_rate": cr.get("rug_rate"),
        # Behavioral microstructure + team-scoring features (Phases A/B/D)
        "team_score_max": ctx.get("team_score_max"),
        "team_score_mean": ctx.get("team_score_mean"),
        "scored_member_count": ctx.get("scored_member_count"),
        "launch_slot_snipe_count": ctx.get("launch_slot_snipe_count"),
        "funder_leader_consistency": ctx.get("funder_leader_consistency"),
        "funder_choreography_n": ctx.get("funder_choreography_n"),
        "verdict": read.verdict,
        "confidence": read.confidence,
    }
    # Pull remaining microstructure/coordination features from the row just written
    fx = conn.execute(
        """SELECT max_same_slot_group, bundled_adjacent_count, buys_first_3_slots
           FROM bc_flow_features WHERE token_mint = ?""",
        (event.token_mint,),
    ).fetchone()
    if fx:
        features["max_same_slot_group"] = fx["max_same_slot_group"]
        features["bundled_adjacent_count"] = fx["bundled_adjacent_count"]
        features["buys_first_3_slots"] = fx["buys_first_3_slots"]
    cc = conn.execute(
        """SELECT bundled_supply_pct, largest_entity_supply_pct
           FROM coin_coordination WHERE token_mint = ? AND phase = 'launch'""",
        (event.token_mint,),
    ).fetchone()
    if cc:
        features["bundled_supply_pct"] = cc["bundled_supply_pct"]
        features["largest_entity_supply_pct"] = cc["largest_entity_supply_pct"]
    # Point-in-time market state (non-recoverable; captured at graduation)
    gm = conn.execute(
        """SELECT holder_count, liquidity_usd, market_cap_usd, txns_total
           FROM graduation_market WHERE token_mint = ?""",
        (event.token_mint,),
    ).fetchone()
    if gm:
        features["holder_count_at_grad"] = gm["holder_count"]
        features["liquidity_usd_at_grad"] = gm["liquidity_usd"]
        features["market_cap_usd_at_grad"] = gm["market_cap_usd"]
        features["txns_total_at_grad"] = gm["txns_total"]
    conn.execute(
        """INSERT INTO graduation_feature_snapshot
               (token_mint, pipeline_version, features_json, snapped_at)
           VALUES (?, 2, ?, ?)
           ON CONFLICT(token_mint) DO UPDATE SET
               features_json = excluded.features_json,
               snapped_at = excluded.snapped_at""",
        (event.token_mint, json.dumps(features), int(time.time())),
    )
    conn.commit()


def _count_proven_buyers(buyers: list[TokenBuyer], conn) -> int:
    """BC buyers with a mature wallet_stats win rate (≥60%, n≥15 gate in DB)."""
    addresses = list({b.wallet_address for b in buyers})
    if not addresses:
        return 0
    placeholders = ",".join("?" * len(addresses))
    row = conn.execute(
        f"""SELECT COUNT(*) FROM wallet_stats
            WHERE address IN ({placeholders})
              AND win_rate IS NOT NULL AND win_rate >= 0.6""",
        addresses,
    ).fetchone()
    return int(row[0]) if row else 0


def _extract_symbol_name(meta: dict | None) -> tuple[str, str]:
    """Pull symbol/name from a Helius v0 token-metadata response.

    The response nests data under onChainMetadata.metadata.data and/or
    legacyMetadata, with offChainMetadata as another fallback. Top-level
    symbol/name do not exist — reading them directly always yields UNKNOWN.
    """
    if not meta:
        return "UNKNOWN", "Unknown"

    # on-chain metadata (Metaplex)
    on_chain = (meta.get("onChainMetadata") or {}).get("metadata") or {}
    on_chain_data = on_chain.get("data") or {}
    symbol = on_chain_data.get("symbol")
    name = on_chain_data.get("name")

    # off-chain metadata (the JSON the URI points to)
    if not symbol or not name:
        off_chain = (meta.get("offChainMetadata") or {}).get("metadata") or {}
        symbol = symbol or off_chain.get("symbol")
        name = name or off_chain.get("name")

    # legacy metadata
    if not symbol or not name:
        legacy = meta.get("legacyMetadata") or {}
        symbol = symbol or legacy.get("symbol")
        name = name or legacy.get("name")

    symbol = (symbol or "").strip() or "UNKNOWN"
    name = (name or "").strip() or "Unknown"
    return symbol, name


async def _dexscreener_pair_addresses(mint: str) -> set[str]:
    """All DexScreener pairAddresses for a mint — these are AMM pool accounts
    that must be excluded from holder analysis. Free API; empty set on miss."""
    url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return set()
                data = await resp.json()
        return {
            p["pairAddress"] for p in (data.get("pairs") or [])
            if isinstance(p.get("pairAddress"), str)
        }
    except Exception:
        return set()


async def _dexscreener_symbol_name(mint: str) -> tuple[str | None, str | None]:
    """Fallback: fetch symbol/name from DexScreener (covers PumpSwap pairs)."""
    import aiohttp
    url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return None, None
                data = await resp.json()
        pairs = data.get("pairs") or []
        if not pairs:
            return None, None
        base = pairs[0].get("baseToken") or {}
        return base.get("symbol"), base.get("name")
    except Exception:
        return None, None


def _parse_bc_holders(accounts: list[dict], total_supply: float | None = None) -> list[dict]:
    """Convert a top-holders response to ranked [{wallet, pct, ui_amount}].

    Caller must pass accounts already filtered of structural (pool/curve/CEX)
    addresses. pct is computed over the REAL total supply when given — summing
    the returned top-100 both misranks holders and inflates every pct.
    """
    if not accounts:
        return []
    total = total_supply or sum(float(a.get("uiAmount") or 0) for a in accounts)
    if total == 0:
        return []
    result = []
    for a in accounts[:20]:
        ui = float(a.get("uiAmount") or 0)
        if ui > 0:
            result.append({
                "wallet": a.get("address", ""),
                "pct": round(ui / total * 100, 2),
                "ui_amount": ui,
            })
    return result


def _print_graduation_alert(
    event: GraduationEvent,
    symbol: str,
    team_cluster: TeamCluster | None,
    sm_buyers: list,
    funder_rep,
    read,
) -> None:
    rugger_line = ""
    if funder_rep and funder_rep.is_known_rugger:
        n = len(funder_rep.graduated_mints)
        rate = funder_rep.rug_rate * 100
        rugger_line = f"\n  ⚠ KNOWN RUGGER — {n} prev graduates, rug rate {rate:.0f}%"

    if team_cluster:
        sniper_tag = " [BC sniper]" if team_cluster.is_bc_sniper else ""
        team_line = (
            f"team {len(team_cluster.member_addresses)} wallets, "
            f"{team_cluster.supply_pct_at_graduation:.1f}% supply{sniper_tag}"
        )
    else:
        team_line = "no team cluster detected"

    factors = " | ".join(read.dominant_factors) if read.dominant_factors else "no strong signals"

    logger.info(
        "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "  GRADUATED $%s — %s%s\n"
        "  %s\n"
        "  Smart money: %d  |  Lag: %ds\n"
        "  Verdict: %s  (confidence %.0f%%)\n"
        "  %s\n"
        "  → %s\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        symbol, event.token_mint[:8], rugger_line,
        team_line,
        len(sm_buyers), event.detection_lag_seconds,
        read.verdict, read.confidence * 100,
        factors,
        read.what_would_change,
    )


# ── entry point ───────────────────────────────────────────────────────────────

async def monitor() -> None:
    """Run the graduation monitor forever."""
    gm = GraduationMonitor()
    logger.info("graduation monitor running — waiting for pump.fun graduations")
    asyncio.create_task(_watchdog_loop())
    # Startup ping doubles as deploy confirmation AND crash-loop detector
    # (repeated pings = launchd KeepAlive is restart-looping the service).
    from src.notifications.telegram import send_message
    asyncio.create_task(send_message("🟢 graduation monitor started"))
    await gm.run()


if __name__ == "__main__":
    import logging as _logging
    _logging.basicConfig(
        level=_logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    asyncio.run(monitor())
