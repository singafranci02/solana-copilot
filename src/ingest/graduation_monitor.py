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
    """Persist the graduation and run full structural analysis."""
    from src.ingest.solana_tracker import SolanaTrackerClient

    conn = get_connection()
    try:
        # Ensure the token row exists (we may have missed the launch)
        token_row = conn.execute(
            "SELECT mint, symbol, created_at FROM tokens WHERE mint = ?", (mint,)
        ).fetchone()

        if token_row is None:
            # DexScreener is the primary (free) name source now
            ds_symbol, ds_name = await _dexscreener_symbol_name(mint)
            symbol = ds_symbol or "UNKNOWN"
            name = ds_name or "Unknown"
            created_at = int(time.time())
            conn.execute(
                """INSERT OR IGNORE INTO tokens
                   (mint, symbol, name, launchpad, created_at, narrative_tags)
                   VALUES (?, ?, ?, 'pump.fun', ?, '[]')""",
                (mint, symbol, name, created_at),
            )
            conn.commit()
            from src.common import supabase_sync as sb
            asyncio.create_task(sb.token(mint, symbol, name, created_at))
            token_created_at = created_at
        else:
            symbol = token_row["symbol"] or "?"
            token_created_at = int(token_row["created_at"]) if token_row["created_at"] else int(time.time())

        now = int(time.time())
        conn.execute(
            """INSERT OR REPLACE INTO graduation_events
               (token_mint, graduated_at, detection_lag_seconds,
                pumpswap_pool_address, bc_top_holders_json)
               VALUES (?, ?, ?, ?, '[]')""",
            (mint, now, detection_lag, pool_address),
        )
        conn.commit()

        # Fetch holders at graduation + reconstruct BC accumulation from the token's
        # full trade history (Solana Tracker, by mint).
        async with SolanaTrackerClient() as st:
            accounts = await st.get_token_holders(mint)
            bc_top_holders = _parse_bc_holders(accounts)
            await _reconstruct_bc(st, mint, bc_top_holders, token_created_at, now, conn)

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
            pumpswap_pool_address=pool_address,
            bc_top_holders=bc_top_holders,
        )

        logger.info(
            "structural analysis: $%s — %d BC buyers, %d top holders",
            symbol, len(buyers), len(bc_top_holders),
        )

        await analyse_graduation(grad_event, buyers, conn, symbol=symbol)

    except Exception:
        logger.exception("graduation analysis failed for %s", mint[:8])
    finally:
        conn.close()


async def analyse_graduation(
    event: GraduationEvent,
    buyers: list[TokenBuyer],
    conn,
    *,
    symbol: str = "?",
) -> None:
    """Full structural analysis pipeline for a graduated token."""
    from src.analyzer.team_detect import build_team_cluster_post_grad
    from src.analyzer.distribution import schedule_distribution_checks
    from src.analyzer.smart_money import (
        get_smart_money_wallets,
        find_smart_money_in_buyers,
        get_funder_reputation,
    )
    from src.strategy.rules import structural_read
    from src.common.cex_wallets import get_all_cex_addresses

    cex_addresses = get_all_cex_addresses(conn)

    team_cluster = build_team_cluster_post_grad(
        event.token_mint, buyers, event.bc_top_holders, cex_addresses
    )

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
    asyncio.create_task(_classify_and_notify(event.token_mint, symbol, read, conn))

    # Schedule price outcome checks at 1h / 4h / 24h from graduation
    # Baseline: ~$69K USD — Pump.fun bonding curve always migrates near this MC
    from src.analyzer.outcome_tracker import schedule_checks
    GRADUATION_MC_USD = 69_000.0
    asyncio.create_task(schedule_checks(event.token_mint, GRADUATION_MC_USD))

    # Schedule distribution checks (team behavior) at 1h / 4h / 24h
    await schedule_distribution_checks(event.token_mint, event.graduated_at)


async def _classify_and_notify(token_mint: str, symbol: str, read, conn) -> None:
    """Classify project vs meme; store it; alert on project AND verdict != SKIP."""
    from src.analyzer.project_classifier import fetch_token_meta, classify_project, _is_real_website
    from src.ingest.solana_tracker import SolanaTrackerClient
    from src.common import supabase_sync as sb
    try:
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
) -> None:
    """F1: reconstruct BC accumulation for top holders + backfill token_buyers."""
    from src.analyzer.bc_reconstruct import (
        reconstruct_bc_holders, upsert_bc_accumulation, to_token_buyers,
    )
    wallets = [h["wallet"] for h in bc_top_holders[:_BC_RECONSTRUCT_TOP_N] if h.get("wallet")]
    if not wallets:
        return
    try:
        profiles, bc_swaps = await reconstruct_bc_holders(
            helius, mint, wallets, token_created_at, graduated_at,
        )
    except Exception as exc:
        logger.debug("BC reconstruction failed for %s: %s", mint[:8], exc)
        return

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

    # Launch-phase coordination: detect bundling in the BC swaps (the canonical
    # rug fingerprint — happens at launch, not post-grad). Same-slot bundles need
    # no extra API; bc_swaps are already in hand.
    _detect_launch_coordination(mint, bc_swaps, conn)


def _detect_launch_coordination(mint: str, bc_swaps, conn) -> None:
    """Run the coordination engine on bonding-curve swaps; store phase='launch'."""
    from src.analyzer.coordination import analyze_coin, upsert_coordination
    if not bc_swaps:
        return
    try:
        cc = analyze_coin(mint, bc_swaps)
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


_FUNDER_RESOLVE_MAX_MEMBERS = 5   # cap funding lookups per graduation (Helius budget)


async def _resolve_funding_source(member_addresses: list[str], conn) -> str | None:
    """F4a: find the team's common SOL funder and persist wallets.funding_source.

    For each member, walk its oldest txs via extract_funding_source. Returns the
    majority funder (excluding 'cex'), or None. Also upserts each member's
    funding_source so the token_buyers→wallets funder path works.
    """
    from src.ingest.rpc import RpcClient, extract_funding_source_rpc
    from collections import Counter

    members = member_addresses[:_FUNDER_RESOLVE_MAX_MEMBERS]
    if not members or not settings.rpc_url:
        return None

    funders: list[str] = []
    try:
        async with RpcClient() as rpc:
            for addr in members:
                try:
                    funder = await extract_funding_source_rpc(rpc, addr)
                except Exception:
                    continue
                if funder:
                    conn.execute(
                        """INSERT INTO wallets (address, funding_source) VALUES (?, ?)
                           ON CONFLICT(address) DO UPDATE SET
                               funding_source = COALESCE(wallets.funding_source, excluded.funding_source)""",
                        (addr, funder),
                    )
                    if funder != "cex":
                        funders.append(funder)
        conn.commit()
    except Exception as exc:
        logger.debug("funding source resolution failed: %s", exc)
        return None

    if not funders:
        return None
    return Counter(funders).most_common(1)[0][0]


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


def _parse_bc_holders(accounts: list[dict]) -> list[dict]:
    """Convert Helius largest-accounts response to ranked [{wallet, pct, ui_amount}]."""
    if not accounts:
        return []
    total = sum(float(a.get("uiAmount") or 0) for a in accounts)
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
    await gm.run()


if __name__ == "__main__":
    import logging as _logging
    _logging.basicConfig(
        level=_logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    asyncio.run(monitor())
